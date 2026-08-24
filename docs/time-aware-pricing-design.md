# Time-Aware Pricing Model for the Flat Router

**Status:** IMPLEMENTED — 5-tier pricing model live in flat_router.py  
**Date:** 2026-08-24  
**Author:** Hermes Agent (manager profile)  
**Felix's insight:** "The ones with a quota have a reset at the end of the week and that any quota we don't burn gets wasted. So as we approach the end of the week, the price has to drop and we have to prefer that over the ones that don't have a quota. NeuralWatt doesn't have a quota, and once we've burnt that up, we need to pay per request. So, we need to price that in as well."

## Implementation Notes (2026-08-24)

### What was implemented
- `PROVIDER_TIER` dict mapping each of 12 providers to their tier (`quota`, `balance`, `flat`, `included`, `per_token`)
- `compute_effective_price(provider, base_rate, context)` function applying the tier-specific formula
- `_compute_time_decay(name)` — reads `quota_cache` weekly window `resets_at` from zai_proxy
- `_compute_quota_health(name)` — reads `used_pct` from weekly quota window
- `_compute_depletion_penalty(name)` — reads `_neuralwatt_quota_snapshot()` for `remaining_usd` / `total_credits_usd`
- `_get_off_peak_factor()` — returns 0.5 during off-peak hours (UTC 10:00–06:00), 1.0 during peak (UTC 06:00–10:00)
- `_get_effective_cost()` modified to call `compute_effective_price()` for T1–T4 providers; T5 (per-token) retains original Kalman `effective_price()` path
- Kill switches: `.disable_time_decay` and `.disable_depletion_penalty` flag files
- Constants: `MIN_EFFECTIVE_PRICE=0.001`, `NW_CORRECTION_FACTOR=0.2762`, `NW_MAX_DEPLETION_PENALTY=2.0`

### What was NOT changed
- `select_provider()` core logic (still sorts by `effective_cost` ascending)
- PriceKalman class (still measures real $/M from traffic)
- Failover chain (candidate list IS the chain)
- No caps or blocks (alert only — pricing changes routing preference, never blocks)

### Test coverage
- 60 new tests in `test_time_aware_pricing.py` covering all 5 tiers, helper functions, edge cases
- 45 existing tests in `test_flat_router.py` continue to pass
- Total: 105 tests pass

---

## 1. Overview

The flat router's `select_provider()` already sorts candidates by `effective_cost` (cheapest first). The current cost calculation uses `PriceKalman.effective_price()` which applies `base_rate × peak_mult × scarcity × health × pace`. What's missing is a **time-decay multiplier** for quota-based providers and a **depletion penalty** for balance-based providers.

This design adds a new deterministic multiplier — `time_factor` — on top of the existing Kalman-measured base rate, following the same ADR-003 principle: deterministic adjustments are multipliers on top of the Kalman smooth, not replacements for it.

### Provider Tier Classification

| Tier | Providers | Cost Structure | Key Insight |
|------|-----------|---------------|-------------|
| T1 — Quota-Based | `ours`, `friend` | Weekly z.ai quota, resets Mondays | Unused quota is wasted → price drops as reset approaches |
| T2 — Balance-Based | `neuralwatt` | $100/mo prepaid + per-token overage | Depleting balance → price rises; at $0 → pay per token |
| T3 — Flat-Rate | `opencode_go` | $10/mo, marginal $0 | effective_price = $0.001/M (floor only) |
| T4 — Included | `ollama_cloud`, `ollama_cloud_2` | Subscription-included | effective_price = $0.001/M (floor only) |
| T5 — Per-Token | `deepinfra`, `ppq`, `telnyx`, `openrouter`, `routstr`, `routstrd` | Pay per token | effective_price = quoted rate (Kalman discovers) |

---

## 2. Pricing Formulas

### TIER 1 — Quota-Based (z.ai `ours`, `friend`)

**Formula:**

```
effective_price = kalman_base_rate × time_decay × quota_health × peak_mult × health_factor
```

Where:
- `kalman_base_rate` = PriceKalman.predict() (current smoothed $/M)
- `time_decay = max(0.01, 1.0 - days_to_reset / 7.0)` — linear decay over the week
- `quota_health = max(0.0, 1.0 - used_pct / 100.0)` — 0 when quota exhausted, 1.0 when fresh
- `peak_mult` = existing peak hours multiplier (3.0 during UTC 6-10, else 1.0)
- `health_factor` = existing graduated failure penalty (1.0 → +inf)

**Key behavior:**

| Time to Reset | used_pct=0% | used_pct=50% | used_pct=80% | used_pct=100% |
|---------------|-------------|--------------|--------------|----------------|
| 7 days (start) | 1.00 × base | 0.50 × base | 0.20 × base | 0 (unavailable) |
| 3 days | 0.57 × base | 0.29 × base | 0.11 × base | 0 (unavailable) |
| 1 day | 0.86 × base | 0.43 × base | 0.17 × base | 0 (unavailable) |
| 1 hour (~0.04d) | 0.99 × base | 0.50 × base | 0.20 × base | 0 (unavailable) |

Wait — let me reconsider. Felix said "as we approach reset, price drops." The formula `1 - days_to_reset/7` gives:
- 7 days to reset → 0 (cheapest? no, that's backwards)

**Corrected formula** — Felix's intent is: early in the week, quota is plentiful, so quota providers are "expensive" (we have plenty of time to use them, no urgency). Late in the week, unused quota will be wasted, so drop the price to prefer them:

```
time_decay = (1.0 - days_to_reset / 7.0)
```

At 7 days to reset: `time_decay = 0` — but that's too aggressive (free at start). We need the INVERSE: early in week = full price (plenty of quota, no urgency to burn), late in week = price drops (use-it-or-lose-it).

Actually re-reading Felix: "as we approach the end of the week, the price has to drop." So at the START of the week (7 days to reset), price should be FULL. At the END (0 days to reset), price should be ~$0.

```
time_decay = days_to_reset / 7.0
```

| Time to Reset | time_decay | Effect |
|---------------|------------|--------|
| 7 days (start) | 1.0 | Full price — quota is fresh, no urgency |
| 5 days | 0.71 | 29% discount |
| 3 days | 0.43 | 57% discount — Felix's example |
| 1 day | 0.14 | 86% discount — aggressive use-it-or-lose-it |
| 1 hour | 0.006 | ~99.4% discount — use every last token |
| 0 (reset moment) | 0.0 | Free (but quota_health gates availability) |
| After reset | 1.0 | Back to full price (fresh quota) |

**Combined with quota_health:**

```
effective_price = kalman_base_rate × (days_to_reset / 7.0) × (1.0 - used_pct/100.0) × peak_mult × health_factor
```

Floor: `max(MIN_EFFECTIVE_PRICE, ...)` — never zero so the router doesn't always-trivially-win when quota is actually available. But when `used_pct >= 100%`, `quota_health = 0` → the provider is excluded by the health gate (returns `inf` via `health_factor` or is simply not in the candidate list).

**Example with current shadow rates:**
- `ours` base_rate ≈ $0.068/M, `friend` ≈ $0.082/M
- 3 days to reset, 50% used: ours = $0.068 × 0.43 × 0.50 = $0.015/M
- 1 day to reset, 20% used: ours = $0.068 × 0.14 × 0.80 = $0.0076/M
- This is CHEAPER than ollama_cloud ($0.001/M floor) — no, $0.0076 > $0.001, so ollama still wins. But ollama is Tier 4 with $0.001/M. The time-decay makes z.ai competitive against per-token providers (NeuralWatt $2.21/M, PPQ $0.80/M) as reset approaches.

**Important:** The time_decay only affects RELATIVE ordering between quota providers and per-token providers. Flat-rate providers (Tier 3/4) at $0.001/M will always be cheaper unless they're unhealthy or rate-limited. The time-decay makes quota providers cheaper than per-token providers as reset approaches, which is exactly Felix's intent: "prefer quota providers over non-quota ones as reset nears."

### TIER 2 — Balance-Based (NeuralWatt)

**Formula:**

```
effective_price = corrected_base_rate × (1.0 + depletion_penalty)
```

Where:
- `corrected_base_rate = kalman_base_rate` (already corrected if Kalman was fed corrected measurements — see §7)
- `depletion_penalty = (1.0 - remaining_balance / initial_balance) × max_penalty`
- `max_penalty = 2.0` (at 0% balance, price triples to reflect per-token overage cost)
- `remaining_balance` from `neuralwatt_quota_entry()` → `remaining_usd` field (or `kwh_remaining / kwh_included` proxy)

**Behavior:**

| Balance Remaining | depletion_penalty | Price Multiple | Rationale |
|-------------------|-------------------|----------------|-----------|
| 100% (full) | 0.0 | 1.0 × base | Normal — plenty of prepaid balance |
| 75% | 0.5 | 1.25 × base | Slightly more expensive |
| 50% | 1.0 | 1.5 × base | Depleting — get more expensive |
| 25% | 1.5 | 1.75 × base | Running low |
| 0% | 2.0 | 2.0 × base | Pure per-token rate — most expensive |

**NeuralWatt 3.6× overcounting correction:**
- NeuralWatt uses energy-based pricing (kWh), not per-token
- 94% of prompt tokens are cached at 5× discount
- Our per-token estimate overcounts by ~5.7× (correction factor ≈ 0.2762 ≈ 1/3.62)
- Corrected rate = recorded_rate × 0.2762
- This correction is applied to the Kalman measurement (what we feed `pk.update()`), NOT as a multiplier on the effective price. The Kalman filter then converges to the corrected base rate over time.

**How to get remaining balance:**
- `_neuralwatt_quota_entry_fn()` returns a dict with:
  - `remaining_usd`: remaining USD balance
  - `total_credits_usd`: initial balance
  - `kwh_remaining`, `kwh_included`: energy allowance
  - `is_exhausted`: bool
  - `is_daily_cap_exceeded`: bool (daily $10 cap)
  - `daily_spent_usd`, `daily_cap_usd`
- When bridge is disabled: falls back to `{used_pct: 0.0, remaining: inf}` — optimistic
- When `is_daily_cap_exceeded`: `used_pct = 100.0` → provider excluded by health gate

### TIER 3 — Flat-Rate (opencode_go)

**Formula:**

```
effective_price = MIN_EFFECTIVE_PRICE  ($0.001/M)
```

- Marginal cost = $0 always (included in $10/mo plan)
- The $0.43/M "estimated equivalent" in `_extract_cost()` is for **SPEND TRACKING** (daily_spend table, burn visibility), NOT for routing decisions
- If rate-limited (429): `_mark_key_exhausted()` fires → health gate excludes → router picks next
- If 401/403: `_mark_key_failure()` fires → graduated health penalty → eventual exclusion

**No time-decay, no balance factor.** The flat-rate provider is always cheapest when healthy. This is correct — Felix's insight is about quota vs. non-quota, not about flat-rate.

### TIER 4 — Included (ollama_cloud, ollama_cloud_2)

**Formula:**

```
effective_price = MIN_EFFECTIVE_PRICE  ($0.001/M)
```

- Same as Tier 3: marginal cost = $0
- If over subscription limit (403 paywall): health gate excludes → router picks next
- The existing `quota_pressure_factor` (RP-PRICING) and `extra_usage_multiplier` (EU-R3) systems handle Ollama's internal quota pressure separately — these are NOT part of this time-aware design. They remain as-is behind their kill switches.

### TIER 5 — Per-Token (deepinfra, ppq, telnyx, openrouter, routstr, routstrd)

**Formula:**

```
effective_price = kalman_base_rate × peak_mult × scarcity × health_factor
```

- No time-decay (no weekly reset to race against)
- No balance depletion penalty (pay per token, balance is tracked separately via credit-pressure systems)
- Standard Kalman price discovery applies — the filter learns the real $/M from measured traffic
- The existing credit-pressure systems (`_OPENROUTER_CREDIT_PRESSURE_ENABLED`, `_DEEPINFRA_CREDIT_PRESSURE_ENABLED`, etc.) remain as-is behind their kill switches. They apply ADDITIONAL pressure on top of the base rate. This design does NOT change them.

---

## 3. How `select_provider()` Uses These Prices

The flat router's `select_provider()` in `flat_router.py` already:

1. **Filters by model match** — `PROVIDER_MODELS[name]` must contain the model
2. **Filters by health** — `_is_provider_healthy(name)` must return True
3. **Evaluates cost** — `_get_effective_cost(name, model_id, difficulty)`
4. **Sorts cheapest first** — `candidates.sort(key=lambda c: c.effective_cost)`

**What changes:** `_get_effective_cost()` needs to incorporate the tier-specific multipliers. Currently it calls `pk.effective_price(peak_mult, scarcity, health, pace_mult)`. The new design adds a `time_factor` parameter:

```python
# PSEUDOCODE — DESIGN ONLY, not for implementation
def _get_effective_cost(name, model, difficulty="medium"):
    base = _get_kalman_base_rate(name)  # existing logic
    
    tier = _classify_provider_tier(name)
    
    if tier == "quota":
        time_decay = _compute_time_decay(name)  # days_to_reset / 7.0
        quota_health = _compute_quota_health(name)  # 1.0 - used_pct/100
        effective = base * time_decay * quota_health * peak_mult * health
    elif tier == "balance":
        depletion = _compute_depletion_penalty(name)  # (1 - rem/init) * 2.0
        effective = base * (1.0 + depletion) * health
    elif tier in ("flat", "included"):
        effective = MIN_EFFECTIVE_PRICE  # $0.001/M
        # health still gates availability, but no cost multiplier
    else:  # per-token
        effective = base * peak_mult * scarcity * health  # existing logic
    
    return max(effective, MIN_EFFECTIVE_PRICE)
```

**The sort is unchanged.** Candidates are still sorted by `effective_cost` ascending. The tier-specific multipliers just change WHAT that cost IS.

---

## 4. How to Get Quota Reset Time

### Source 1: `quota_cache` (primary, already populated)

The background `_refresh_loop()` calls `_fetch_quota_windows(key)` every 5 minutes, which hits `QUOTA_URL` (`https://api.z.ai/api/monitor/usage/quota/limit`) and parses the response. Each window dict contains:

```python
{
    "name": "weekly",           # or "5-hour", "monthly"
    "type": "CREDIT_LIMIT",
    "used_pct": 67,             # 0-100
    "resets_at": 1724889600,    # Unix timestamp of next reset
    "window_hours": 168         # window duration
}
```

The `resets_at` field comes from the API's `nextResetTime` (milliseconds → seconds). This is the **authoritative source** for when the weekly quota resets.

**To get days_to_reset:**

```python
# PSEUDOCODE
def _compute_time_decay(key_name):
    windows = quota_cache.get(key_name, ([], 0.0))[0]
    weekly = next((w for w in windows if w.get("name") == "weekly"), None)
    if weekly and weekly.get("resets_at"):
        seconds_to_reset = weekly["resets_at"] - time.time()
        days_to_reset = max(0.0, seconds_to_reset / 86400.0)
        return max(0.01, days_to_reset / 7.0)
    return 1.0  # unknown reset time → no decay (full price)
```

### Source 2: `/v1/dispatch_gate` endpoint (secondary)

The proxy's HTTP handler exposes `/v1/dispatch_gate` which includes `quota_state` with `used_pct` and `locked` fields, but does NOT currently expose `resets_at`. However, the underlying `quota_cache` data is available in-process.

### Source 3: `zai_state.json` (not currently used for reset times)

`STATE_FILE = ~/.hermes/bot/zai_proxy_state.json` stores proxy state but does not currently include quota window reset times. The `quota_cache` in-memory dict is the live source.

**Design decision:** Use `quota_cache` directly (in-process, no I/O). The `resets_at` field is already parsed by `_parse_limit_entry()`. The flat router can access it via `_resolve("quota_cache")` or by passing it through from the proxy context.

---

## 5. How to Get NeuralWatt Remaining Balance

### The Balance Bridge

`_neuralwatt_quota_entry_fn` is loaded from `src.balance_collectors` at import time. It reads the latest row from `provider_balances` table in `api_burn.db` (written by the `balance_collectors --provider neuralwatt` cron job every 5 minutes).

**Returns:**

```python
{
    "used_pct": 42.5,                # kwh_used / kwh_included * 100
    "remaining": 7.7,                # kwh_remaining
    "total": 13.33,                  # kwh_included (monthly allowance)
    "is_exhausted": False,           # kwh_remaining <= 0
    "is_daily_cap_exceeded": False,  # daily_spent > $10
    "daily_spent_usd": 3.42,
    "daily_cap_usd": 10.0,
    "remaining_usd": 65.00,          # remaining USD balance
    "total_credits_usd": 100.00,     # initial balance ($100/mo Pro plan)
    "cost_usd_lifetime": 35.00,
    "subscription_status": "active",
    "period_end": "2026-09-01",
}
```

**For the depletion penalty:**

```python
# PSEUDOCODE
def _compute_depletion_penalty(name):
    if name == "neuralwatt":
        entry = _neuralwatt_quota_snapshot()
        remaining = entry.get("remaining_usd", 0.0)
        initial = entry.get("total_credits_usd", 100.0)
        if initial <= 0:
            return 2.0  # max penalty (unknown balance → conservative)
        depletion = max(0.0, 1.0 - remaining / initial)
        return depletion * 2.0  # max_penalty = 2.0
    return 0.0
```

**When bridge is disabled:** Falls back to `{used_pct: 0.0, remaining: inf}` → `remaining_usd` is missing → `remaining = 0.0`, `initial = 100.0` → `depletion = 1.0` → `penalty = 2.0` → price = 3× base. This is CONSERVATIVE (treats unknown balance as depleted), which is correct — we don't want to route to NeuralWatt at full price if we can't verify the balance.

**Alternative:** Use `kwh_remaining / kwh_included` as the depletion fraction (energy-based, more accurate for NeuralWatt's actual billing model). This avoids the USD→kWh conversion ambiguity.

---

## 6. How the Kalman Filter Incorporates This

### Key Principle (ADR-003)

The Kalman filter measures the **smooth base rate** ($/M) from real traffic. Time-decay, scarcity, health, and peak multipliers are **deterministic functions applied ON TOP** — they are NOT Kalman inputs. This design adds `time_decay` and `depletion_penalty` as two new deterministic multipliers, following the same principle.

### What the Kalman Tracks

`PriceKalman` state: `[base_rate, velocity]`
- `base_rate` — current estimated $/M (smoothed from measurements)
- `velocity` — rate of change in $/M per update cycle

### How Measurements Are Fed

After each successful request, `_update_kalman_after_request()` computes:
```
measured_rate = (cost_usd / total_tokens) * 1_000_000
pk.update(measured_rate)
```

For NeuralWatt, `cost_usd` comes from `_extract_cost()` which uses `NEURALWATT_RATES × tokens`. This is the **uncorrected** per-token estimate. The correction factor (0.2762) is applied in `_estimate_cost_usd()` but NOT in `_extract_cost()` — see §7.

### Time-Decay as a Multiplier

The time-decay is NOT fed to the Kalman. It's applied at query time in `_get_effective_cost()`:

```
effective_price = pk.predict() × time_decay × quota_health × peak_mult × health
```

This means:
- The Kalman continues to learn the true $/M from real traffic
- The time-decay makes the provider look CHEAPER to the router as reset approaches
- When the quota resets, `time_decay` jumps back to 1.0 — the effective price returns to full, but the Kalman's base_rate estimate is UNCHANGED (it was learning the true cost the whole time)

### Depletion Penalty as a Multiplier

Similarly, the NeuralWatt depletion penalty is applied at query time:
```
effective_price = pk.predict() × (1.0 + depletion_penalty) × health
```

The Kalman tracks the per-token rate; the depletion penalty makes NeuralWatt look MORE EXPENSIVE as the balance depletes, which is correct — we want to preserve the prepaid balance and prefer cheaper alternatives.

---

## 7. What Happens When a Quota Provider Resets

### At Reset Moment

1. The z.ai API returns `used_pct = 0` for the weekly window
2. `quota_cache` updates on next poll (within 5 minutes)
3. `time_decay` jumps from ~0.0 to ~1.0 (7 days to next reset)
4. `quota_health` jumps from 0.0 to 1.0 (0% used)
5. `effective_price` = `base_rate × 1.0 × 1.0 × peak × health` = full price
6. The provider is immediately available at full price

### Kalman Continuity

The Kalman filter's `base_rate` estimate is **unaffected by the reset**. It has been learning the true $/M from real traffic throughout the week. The reset only changes the deterministic multipliers. This is by design — the true cost per token doesn't change just because the quota reset.

### Transition Smoothness

The price jump from ~$0 to full price is instant (step function). This is consistent with ADR-003's treatment of deterministic changes as instant steps. The Kalman smooths the MEASUREMENT, not the multiplier.

---

## 8. NeuralWatt Correction Factor: Why It's Not Applying

### The Problem

The correction factor (`get_neuralwatt_cost_correction_factor()`) IS loaded and IS used in `_estimate_cost_usd()` (line 2838-2840 of zai_proxy.py). However, `_extract_cost()` (line 3378-3411) does NOT use the correction factor.

### Two Separate Cost Functions

There are two cost estimation paths:

1. **`_estimate_cost_usd()`** (line 2809) — used by `_record_spend()` for the `daily_spend` table. This function DOES apply the correction factor (line 2837-2845).

2. **`_extract_cost()`** (line 3266) — used by:
   - `_log_api_call()` for the `api_calls` table (the authoritative per-call cost)
   - The flat router's `_update_kalman_after_request()` for Kalman measurements
   - `_record_spend()` when `actual_cost` is provided (which it is, from `_extract_cost`)

### The Bug

`_extract_cost()` for NeuralWatt (lines 3378-3411) computes `raw_cost` from `NEURALWATT_RATES × tokens` but does NOT multiply by the correction factor. So:

- `api_calls.cost_usd` = uncorrected (overcounted ~3.6×)
- `daily_spend.spend_usd` = uncorrected (because `_record_spend` uses `actual_cost` from `_extract_cost` when available, line 2872: `cost = actual_cost if actual_cost is not None else _estimate_cost_usd(...)`)
- `PriceKalman.update()` = receives uncorrected rate → Kalman base_rate is inflated ~3.6×

### Why It Matters for Routing

The Kalman base_rate for NeuralWatt is currently ~$2.21/M (seed rate). The REAL rate after correction should be ~$2.21 × 0.2762 = ~$0.61/M. With the correction applied:
- The Kalman would converge to ~$0.61/M instead of ~$2.21/M
- NeuralWatt would look 3.6× cheaper to the router
- This changes routing decisions: NeuralWatt at $0.61/M is competitive with PPQ ($0.80/M) and DeepInfra ($1.30/M)

### The Fix (Design Only — for Felix to approve)

Add the correction factor to `_extract_cost()`'s NeuralWatt path:

```python
# PSEUDOCODE — DESIGN ONLY
if provider == "neuralwatt":
    # ... existing rate computation ...
    raw_cost = (uncached_toks * input_rate + cached_toks * cached_rate + 
                completion_toks * output_rate) / 1_000_000
    
    # Apply the correction factor (3.6× overcounting fix)
    nw_correction = 1.0
    if _neuralwatt_cost_correction_fn is not None:
        try:
            nw_correction = float(_neuralwatt_cost_correction_fn() or 1.0)
        except Exception:
            nw_correction = 1.0
    corrected_cost = raw_cost * nw_correction
    return (corrected_cost, cost_source)
```

This ensures:
- `api_calls.cost_usd` is corrected → accurate spend tracking
- `daily_spend.spend_usd` is corrected (since `_record_spend` uses `actual_cost`)
- Kalman measurements are corrected → base_rate converges to the real $/M
- The correction factor is cached (10-min TTL) and falls back to 1.0 on any error

---

## 9. Diagnosis: Why NeuralWatt Serves deepseek-v4-flash Instead of opencode_go

### Root Cause: Early-Exit Path Bypasses Flat Router for deepseek Models

At line 4939 of `zai_proxy.py`:

```python
if original_model and original_model.startswith(("deepseek/", "qwen", "minimax", "mimo")):
    response_buffer = bytearray()
    if OPENCODE_GO_KEY and self._try_opencode_go(body, original_model, response_buffer, t0):
        return
    if self._try_external_failover(body, original_model, response_buffer, t0):
        return
    self.send_response(503)
    ...
    return  # <-- EARLY EXIT: flat router at line 4974 NEVER REACHED
```

This early-exit path fires for ALL `deepseek/*` models, **before** the flat router gets a chance to run. It tries opencode_go first, then external failover.

### Why opencode_go Fails for deepseek

`_try_opencode_go()` (line 4296) sets `og_model = model or "glm-5.2"` — it uses the model name **verbatim**. When called from the early-exit path at line 4941, `model` is `original_model` = `"deepseek/deepseek-v4-flash"`.

But OpenCode.ai expects the **bare** model ID `"deepseek-v4-flash"` (per `_PROVIDER_MODEL_NAMES["opencode_go"]["deepseek/deepseek-v4-flash"] = "deepseek-v4-flash"` at line 667). The `_try_opencode_go()` method does NOT consult `_PROVIDER_MODEL_NAMES` — it sends the raw model string.

So the request to OpenCode.ai sends `"model": "deepseek/deepseek-v4-flash"` which the API likely rejects (400 or 404), causing `_try_opencode_go` to return `False`, and the request falls through to `_try_external_failover()` which routes to NeuralWatt.

### The Flat Router Would Handle This Correctly

The flat router's `_resolve_model_for_provider()` (line 553 of flat_router.py) DOES consult `_PROVIDER_MODEL_NAMES` and would translate `deepseek/deepseek-v4-flash` → `deepseek-v4-flash` for opencode_go. The dispatch would succeed.

But the early-exit at line 4939 **prevents the flat router from ever seeing deepseek models**.

### Fix Options (Design Only)

**Option A (minimal):** Fix `_try_opencode_go()` to use `_PROVIDER_MODEL_NAMES` for model translation:
```python
# PSEUDOCODE
og_model = model or "glm-5.2"
model_map = _PROVIDER_MODEL_NAMES.get("opencode_go", {})
og_model = model_map.get(og_model, og_model)  # translate if mapping exists
```

**Option B (structural):** Remove the early-exit path for deepseek models (lines 4939-4949) and let the flat router handle them. The flat router already has `deepseek/deepseek-v4-flash` in `PROVIDER_MODELS["opencode_go"]` and would correctly translate the model name. This is the cleaner solution but changes the request path for all deepseek models.

**Option C (belt-and-suspenders):** Both A and B. Fix the model translation in `_try_opencode_go()` (so it works from any call site) AND remove the early-exit (so the flat router drives all routing).

**Recommendation:** Option C. The early-exit paths are legacy from before the flat router. The flat router is now LIVE (Phase 3 cutover complete) and should handle ALL models. The early-exits should be removed for any model that `PROVIDER_MODELS` covers. The model translation fix in `_try_opencode_go()` is a safety net.

---

## 10. Implementation Plan (For Felix's Approval)

### Phase 1: Fix the NeuralWatt correction factor (§8)
- Add correction factor to `_extract_cost()` NeuralWatt path
- This is a one-line multiplication, low risk, high impact
- Kalman will start converging to the real rate within ~10 updates

### Phase 2: Fix the opencode_go deepseek routing (§9)
- Add model translation to `_try_opencode_go()` (Option A)
- Remove or gate the early-exit path for deepseek models (Option B)
- Test: verify deepseek-v4-flash requests route to opencode_go when it's healthy

### Phase 3: Implement time-decay for quota providers (§2, Tier 1)
- Add `_compute_time_decay()` function to `flat_router.py`
- Read `resets_at` from `quota_cache` weekly window
- Apply as multiplier in `_get_effective_cost()`
- Kill switch: `.disable_time_decay` flag file

### Phase 4: Implement depletion penalty for NeuralWatt (§2, Tier 2)
- Add `_compute_depletion_penalty()` function to `flat_router.py`
- Read `remaining_usd` / `total_credits_usd` from `_neuralwatt_quota_snapshot()`
- Apply as multiplier in `_get_effective_cost()`
- Kill switch: `.disable_depletion_penalty` flag file

### Phase 5: Set flat-rate floor for Tier 3/4 (§2, Tiers 3-4)
- Override `_get_effective_cost()` to return `MIN_EFFECTIVE_PRICE` for `opencode_go`, `ollama_cloud`, `ollama_cloud_2`
- Currently these providers have seed rates ($0.40/M) that make them look expensive vs. their true $0 marginal cost
- This is the simplest change — just return $0.001/M for these providers

### Phase 6: Validation
- Shadow-log the time-aware costs alongside current costs
- Compare routing decisions: do quota providers get preferred as reset approaches?
- Verify NeuralWatt routes differently after correction factor fix
- Monitor for 1 week to observe the full reset cycle

---

## 11. Edge Cases and Safety

### Quota provider with unknown reset time
If `resets_at` is 0 or missing from the weekly window: `time_decay = 1.0` (no decay, full price). Safe default — we don't discount what we can't verify.

### NeuralWatt bridge disabled
`_neuralwatt_quota_snapshot()` returns `{used_pct: 0.0, remaining: inf}`. The depletion penalty function should detect missing `remaining_usd` and apply `max_penalty` (conservative — treat as depleted). This prevents routing to NeuralWatt at full price when we can't verify the balance.

### Both z.ai keys at 100% quota
`quota_health = 0` for both → effective_price = 0 × base = 0. But the health gate (`_is_provider_healthy()`) excludes them BEFORE cost evaluation. The `quota_health` multiplier is a belt-and-suspenders — the health gate is the primary exclusion mechanism.

### Flat-rate provider rate-limited (429)
`_mark_key_exhausted()` fires → `_is_key_healthy("opencode_go")` returns False → health gate excludes → router picks next. The $0.001/M price is irrelevant when the provider is excluded.

### Time decay makes quota provider cheaper than flat-rate
Can `time_decay × quota_health × base_rate < $0.001/M`? 
- Minimum: `0.01 × 0.01 × $0.068 = $0.0000068/M` — yes, technically below $0.001/M.
- The `max(MIN_EFFECTIVE_PRICE, ...)` floor in `effective_price()` prevents this.
- So quota providers at end-of-week with low usage will be priced at $0.001/M — same as flat-rate. The sort order between them is then arbitrary (both at floor). This is acceptable — both are essentially free.

### Kill switches
Each new multiplier gets its own flag file:
- `~/.hermes/bot/.disable_time_decay` — disables quota time-decay
- `~/.hermes/bot/.disable_depletion_penalty` — disables NeuralWatt depletion penalty
- Existing `~/.hermes/bot/.disable_flat_router` — disables the entire flat router (rollback)

---

## 12. Current System State (2026-08-24)

| Provider | Tier | Current effective $/M | Target effective $/M | Notes |
|----------|------|-----------------------|-----------------------|-------|
| ours | T1 (quota) | $0.068 | $0.068 × time_decay × quota_health | LOCKED until Aug 27 (100% weekly) |
| friend | T1 (quota) | $0.082 | $0.082 × time_decay × quota_health | LOCKED until Aug 27 (100% weekly) |
| ollama_cloud | T4 (included) | $0.017 (shadow) | $0.001 (floor) | Currently priced above true marginal cost |
| ollama_cloud_2 | T4 (included) | $0.017 (shadow) | $0.001 (floor) | Same |
| opencode_go | T3 (flat) | $0.18 (shadow) | $0.001 (floor) | Key activated, serving glm-5.3 only |
| neuralwatt | T2 (balance) | $1.43 (shadow) | $0.61 (corrected) × (1 + depletion) | 3.6× overcounting inflates price |
| deepinfra | T5 (per-token) | $1.30 | $1.30 (unchanged) | |
| ppq | T5 (per-token) | $0.80 | $0.80 (unchanged) | |
| telnyx | T5 (per-token) | $5.40 | $5.40 (unchanged) | |
| openrouter | T5 (per-token) | $1.50 | $1.50 (unchanged) | |

### Impact of This Design on Current Routing

With z.ai keys LOCKED (100% weekly quota until Aug 27):
- T1 providers are excluded by health gate → no time-decay effect yet
- Aug 27 reset: T1 providers return at full price, then time-decay begins
- NeuralWatt correction fix: price drops from $1.43/M to ~$0.61/M → more competitive
- opencode_go floor fix: price drops from $0.18/M to $0.001/M → always cheapest when healthy
- deepseek routing fix: opencode_go serves deepseek-v4-flash instead of NeuralWatt → saves ~$0.61/M per request

---

## 13. Summary of Changes (All Design-Only — Felix Approves Before Implementation)

1. **`_extract_cost()` NeuralWatt path** — multiply by `get_neuralwatt_cost_correction_factor()` (fixes 3.6× overcounting)
2. **`_try_opencode_go()` model translation** — consult `_PROVIDER_MODEL_NAMES["opencode_go"]` for model name mapping
3. **Remove/gate deepseek early-exit** — let the flat router handle deepseek models (it already has correct model mappings)
4. **`_get_effective_cost()` in flat_router.py** — add tier classification and apply:
   - T1: `base × time_decay × quota_health × peak × health`
   - T2: `base × (1 + depletion_penalty) × health`
   - T3/T4: `MIN_EFFECTIVE_PRICE` ($0.001/M)
   - T5: existing `base × peak × scarcity × health`
5. **`_compute_time_decay()`** — new function reading `quota_cache` weekly window `resets_at`
6. **`_compute_depletion_penalty()`** — new function reading `_neuralwatt_quota_snapshot()` balance fields
7. **Kill switches** — `.disable_time_decay`, `.disable_depletion_penalty` flag files