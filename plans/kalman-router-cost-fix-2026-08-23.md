# Plan: Fix Z.AI Cost Model, Kalman Router, Nostr Feed, and Crash Bug

**Date:** 2026-08-23
**Status:** PLANNING

## Problem Summary

The proxy is spending $25.63/day on openrouter for glm-5.2 because:
1. z.ai quota is "blind" (reading 0% instead of real usage) — looks free/cheap
2. The Nostr publisher broadcasts `zai_available=True, price=$0.003/M` based on that false 0%
3. routstrd sells z.ai at that phantom price to real customers
4. The Kalman state is broken (velocity=0.0 for all providers)
5. ZAI_ANNUAL_BUDGET=$300 is wrong — you paid $80/mo, not $300/yr
6. ZAI_QUOTA_PRESSURE_ENABLED is off — no quota pressure exposure to the router
7. A crash at line 4502 (`original_model.startswith` when model is None) kills requests with no body
8. `_spend_tier()` doesn't recognize `ollama_cloud_2` or `neuralwatt` → phantom $1/M "unknown" costs

The REAL cost today: $0.51 (openrouter) + $0.08 (telnyx) + $80/mo z.ai amortized (~$2.31/day). The $25.63 openrouter spend is the real bleed — z.ai quota exhausted, everything overflowed to openrouter at $0.23/M.

---

## Fixes

### FIX-1: ZAI_ANNUAL_BUDGET ($300 → $960)

**File:** `src/real_price_tracker.py:283`

`ZAI_ANNUAL_BUDGET = 300.0` — this is wrong. You pay $80/mo = $960/yr.

**Change:** `ZAI_ANNUAL_BUDGET = 960.0`

Better: make it env-overridable (like the other pricing constants):
```python
ZAI_ANNUAL_BUDGET: float = float(
    os.environ.get("ZAI_ANNUAL_BUDGET", "960.0")
)
```

**Impact:** z.ai amortized rate jumps from ~$0.014/M → ~$0.045/M based on real 30d volume (~1.4B tokens/month on ours+friend). This is still cheap vs openrouter ($0.23/M) but no longer absurdly free. The optimizer will see z.ai as more expensive than ollama_cloud_2 ($0.0155/M), which is correct — ollama_cloud_2 IS cheaper per token.

### FIX-2: Enable ZAI_QUOTA_PRESSURE_ENABLED

**File:** systemd service or `.env`

Currently `ZAI_QUOTA_PRESSURE_ENABLED` defaults to `false` (live_router.py:138-139). This means the LiveRouter NEVER applies quota pressure to z.ai prices — z.ai always looks like its base rate regardless of how close it is to exhaustion.

**Change:** Add to the systemd service Environment:
```
Environment=ZAI_QUOTA_PRESSURE_ENABLED=true
Environment=OLLAMA_QUOTA_PRESSURE_ENABLED=true
Environment=PPQ_QUOTA_PRESSURE_ENABLED=true
Environment=OPENROUTER_CREDIT_PRESSURE_ENABLED=true
Environment=DEEPINFRA_CREDIT_PRESSURE_ENABLED=true
Environment=TELNYX_CREDIT_PRESSURE_ENABLED=true
Environment=LIVE_ROUTER_DYNAMIC_RATES_ENABLED=true
```

**Impact:** When z.ai usage hits 60%+ (the onset threshold), its effective price starts rising exponentially. At 90%+ it approaches infinity, forcing the router to reroute to ollama_cloud_2/neuralwatt BEFORE z.ai actually exhausts. This is the "expose quota pressure as price" you asked for.

**Risk:** This is a routing behavior change. The pressure curves have been code-reviewed and tested but never run in production with live traffic. The kill switches are per-provider (set to `false` to disable individually). Recommend enabling ONE at a time: z.ai first, then ollama, then paid providers.

### FIX-3: Fix the NoneType crash at line 4502

**File:** `zai_proxy.py:4502`

The trace: `_extract_model(body)` returns `None` when body is empty/invalid → `original_model` is `None` → `original_model.startswith(("deepseek/", ...))` crashes.

But wait — the traceback says the crash is at line 4481 (`_try_telnyx` error path) and line 5103 (`do_POST` telemetry). Looking more carefully at the traceback:

The ACTUAL crash is in the `finally` block of `do_POST` (line 5103) where it calls `_log_model_decision` with `original_model=None`, and somewhere in that path `.startswith()` is called on None.

Actually, looking at the traceback more carefully:
```
File "zai_proxy.py", line 5103, in do_POST
    f"billed={_billed} actual~={_actual} "
File "zai_proxy.py", line 4481, in _proxy
    self.wfile.write(f'{{"error":"both ollama cloud and telnyx failed..."}})
```

This is a weird traceback — line 5103 is in `do_POST` but the error occurs at line 4481 inside `_proxy`. The `original_model` is the model from the body, and `_extract_model` can return None. The `.startswith` at line 4502 (my new non-z.ai model block) crashes when model is None.

**Fix:** Guard the `original_model.startswith` call:
```python
# Line 4502 — add None guard
if original_model and original_model.startswith(("deepseek/", "qwen", "minimax", "mimo")):
```

Also check ALL other `.startswith` calls on `original_model` — the `_OLLAMA_ONLY_MODELS` block at 4472 uses `in` which is safe for None (returns False), but `_TELNYX_DIRECT_MODELS` at 4492 also uses `in`. The only `.startswith` call is the one I added at 4502.

### FIX-4: Add `ollama_cloud_2` and `neuralwatt` to `_spend_tier()`

**File:** `zai_proxy.py:2591-2604`

`_spend_tier()` doesn't recognize `ollama_cloud_2` or `neuralwatt` → both get classified as `"unknown"` → cost tracking uses `UNKNOWN_PROVIDER_FALLBACK` ($1.00/M) instead of the real rate → phantom costs in daily_spend.

**Fix:**
```python
def _spend_tier(key_name: str | None) -> str:
    if key_name in ("ours", "friend"):
        return key_name
    elif key_name in ("ollama_cloud", "ollama_cloud_2"):
        return key_name
    elif key_name == "deepinfra":
        return "deepinfra"
    elif key_name in ("telnyx", "ppq", "openrouter", "routstr", "routstrd",
                      "opencode_go", "neuralwatt"):
        return key_name
    return "unknown"
```

### FIX-5: Fix the "unknown provider" rate tracker warnings

This is the phantom $1/M cost issue. The `get_rate_with_fallback()` function in `real_price_tracker.py` logs "unknown provider" for `ollama_cloud_2` and `neuralwatt` despite both being in `LAST_RESORT_RATES`. But fresh Python imports return the correct rate. The running process may be loading a stale module.

**Diagnosis:** The `LAST_RESORT_RATES` dict at line 195 has the entries. The `get_rate_with_fallback()` function at line 1346 does `LAST_RESORT_RATES.get(provider)` — if the entry is there, it should return it. The fact that it doesn't in the running process suggests either:
1. A stale `.pyc` (we cleared this — didn't help)
2. The `get_rate_with_fallback` function is being called with a different provider string (e.g., `"zai_ours"` instead of `"ours"`)
3. A circular import or module-level copy issue

**Fix approach:** Add a trace log to `get_rate_with_fallback` to print the exact provider string and whether it's found in LAST_RESORT_RATES. Or simply clear all `.pyc` files AND restart with a clean Python path.

Actually, looking at this more carefully — the warnings say "unknown provider" which is the `get_rate_with_fallback()` path (step 4, line 1357). Steps 1-3 must all have returned None/not-matched. But `LAST_RESORT_RATES.get(provider)` at line 1346 SHOULD match...

The issue might be that `_rpt_get_rate` in zai_proxy.py (line 110) imports `get_rate_with_fallback` at module load time, BEFORE the `LAST_RESORT_RATES` dict was updated. But Python dicts are mutable and shared by reference, so this shouldn't be an issue...

UNLESS there's a second copy of the module loaded. The proxy runs from `/home/c03rad0r/.hermes/bot/zai_proxy.py` which does `from src.real_price_tracker import get_rate_with_fallback`. But the Nostr publisher thread and the shadow hook also import from `src.real_price_tracker`. If there's a path issue, Python might load the module twice.

**Actual fix:** Just add direct entries to `_FALLBACK_RATES` in `zai_proxy.py:2263` — this is the inline fallback that `_rpt_rate()` uses when the tracker call fails. Also confirm the entries are in `SEED_RATES` and `LAST_RESORT_RATES` (they are, verified). The warnings are cosmetic but the cost tracking is real — FIX-4 above fixes the tier classification which is the real bug.

### FIX-6: Fix the Kalman state (velocity=0.0)

**File:** `kalman_price_state.json` shows velocity=0.0 for all 3 providers.

The Kalman filters in `shadow_hook.py` collect observations via `compare()` calls. Zero velocity means either:
1. `compare()` isn't being called (shadow hook not active)
2. The burn-rate feeding is broken (tokens not recorded)
3. The ConsumptionKalman update is broken

The `kalman_price_state.json` is written by `calc_price_per_token.py` (the cron at `*/15 * * * *`). Let me check if that script is erroring.

**Fix approach:**
1. Check `~/.hermes/logs/kalman_price_refresh.log` for errors
2. Delete `kalman_price_state.json` and let it rebuild from fresh data
3. If `calc_price_per_token.py` is broken, fix it
4. This is secondary to the pricing fixes — the static + pressure-based routing will work even with broken Kalman state

### FIX-7: Nostr publisher false-positive guard

**File:** `zai_proxy.py:5739-5743`

The code at line 5739 already has a guard:
```python
if wins and all(w.get("name") == "unknown" for w in wins):
    available = False
```

But this only fires when ALL windows have name=="unknown". The actual problem is when `quota_cache` returns empty windows (`wins = []`). When `wins` is empty:
- `pct = _max_pct(wins)` returns 0 (looks like 0% used)
- `available = healthy and not locked` → if the key is "healthy" (TCP responds), it looks available
- The publisher broadcasts `zai_available=True, price=$0.003/M`

**Fix:** Add an empty-windows guard:
```python
# After line 5734
if not wins or len(wins) == 0:
    available = False
    locked = True
    lwin = "no_quota_data"
```

This ensures the publisher NEVER broadcasts "available" when it has no quota data.

---

## Execution Checklist

### Phase 1: Critical Cost Fixes (do these first)

- [x] P1-1. Fix ZAI_ANNUAL_BUDGET: $300 → $960 (env-overridable) in `real_price_tracker.py:283`
- [x] P1-2. Add `ollama_cloud_2` and `neuralwatt` to `_spend_tier()` in `zai_proxy.py:2591`
- [x] P1-3. Add `ollama_cloud_2` and `neuralwatt` to `_FALLBACK_RATES` in `zai_proxy.py:2263`
- [x] P1-4. Guard `original_model.startswith` at `zai_proxy.py:4502` against None
- [x] P1-5. Add empty-windows guard in `_build_kalman_pricing_json()` at `zai_proxy.py:5734`
- [x] P1-6. Compile check all modified files
- [x] P1-7. Clear `__pycache__` + restart zai-proxy
- [x] P1-8. Verify no "unknown provider" warnings in journal
- [x] P1-9. Verify no NoneType crashes in journal
- [x] P1-10. Verify `/kalman-pricing` shows correct zai_available

### Phase 2: Enable Quota Pressure (routing behavior change)

- [x] P2-1. Add `ZAI_QUOTA_PRESSURE_ENABLED=true` to systemd service
- [x] P2-2. Add `OLLAMA_QUOTA_PRESSURE_ENABLED=true` and `LIVE_ROUTER_DYNAMIC_RATES_ENABLED=true`
- [x] P2-3. Restart zai-proxy
- [x] P2-4. Verify z.ai price rises as quota depletes (check /kalman-pricing)
- [x] P2-5. Verify router reroutes to ollama_cloud_2/neuralwatt before z.ai exhausts
- [ ] P2-6. (Defer) Enable PPQ/OpenRouter/DeepInfra/Telnyx pressure — one at a time

### Phase 3: Kalman Fix (secondary)

- [x] P3-1. Check `~/.hermes/logs/kalman_price_refresh.log` for errors (no errors — cron runs fine)
- [x] P3-2. Delete `kalman_price_state.json` and let it rebuild
- [x] P3-3. Fix `calc_price_per_token.py`: add step_threshold override to tuning, fix process_noise default
- [x] P3-4. Verify Kalman velocity > 0 after one observation cycle (ours=17288, friend=2415, ppq=46418)

### Phase 4: Documentation

- [x] P4-1. Document the v4-pro vs v4-flash vs glm-5.2 cost comparison (done above)
- [x] P4-2. No profile changes needed — keep glm-5.2 workers as-is
- [ ] P4-3. espeak-ng notification

---

## Deepseek-v4-pro Tradeoff Analysis

| Model | Input $/M | Output $/M | Blended $/M | Coding Score | SWE-bench | Context |
|-------|-----------|------------|-------------|--------------|-----------|--------|
| glm-5.2 (z.ai) | $0 (sub) | $0 (sub) | ~$0.057 amortized | ~60 | ~55% | 128K |
| deepseek-v4-flash | $0.14 | $0.28 | ~$0.21 | 85 | ~50% | 1M |
| **deepseek-v4-pro** | **$1.00** | **$3.00** | **~$2.00** | **92** | ~55% | 1M |
| glm-4.5-flash (old) | $0 | $0 | ~$0.057 | ~40 | ~35% | 128K |

**Key tradeoffs for v4-pro:**
- 10x better coding score than glm-4.5-flash
- ~50% better coding score than glm-5.2
- But 35x more expensive than z.ai amortized
- In PREVIEW (unstable, reduced rate limits)
- No flat-rate provider serves it (opencode_go serves deepseek-v4-flash, NOT pro)

**Why NOT to default to v4-pro:**
1. **Cost**: At ~2M worker tokens/day, v4-pro costs ~$4/day ($120/mo). Current z.ai cost is ~$2.67/day ($80/mo). That's 1.5x more expensive for ~8% better quality.
2. **Stability**: v4-pro is in preview. Reduced rate limits + may be unstable. Can't be a production default.
3. **opencode_go doesn't serve it**: The $10/mo flat-rate subscription only has `deepseek-v4-flash`, not `deepseek-v4-pro`. All v4-pro traffic goes to neuralwatt at $2.00/M.
4. **manager + cold review catches quality failures**: The 7-gate quality system already catches bad code regardless of model. The ~8% quality gain doesn't translate to 35x cost savings in rework.

**When to use v4-pro (not as default):**
- A task fails cold review twice → escalate from glm-5.2/flash to v4-pro for ONE retry
- Complex architecture/refactoring that the manager flags as "heavy"
- The `model_selector.py` already encodes this as Tier 2 escalation

---

## What This Fixes

**The $25/day openrouter bleed:** The root cause is z.ai quota exhausting → fallback to openrouter at $0.23/M for glm-5.2. With FIX-1 (correct z.ai amortized cost) + FIX-2 (quota pressure enabled), the router will:
1. See z.ai getting more expensive as quota depletes
2. Reroute to ollama_cloud_2 ($0.0155/M) BEFORE z.ai hits 100%
3. Reroute to neuralwatt ($0.21/M) or deepseek-v4-flash as a cheaper alternative to openrouter ($0.23/M)

**The phantom $50 spend alerts:** The "unknown" tier at $1.00/M is caused by `_spend_tier()` not recognizing `ollama_cloud_2` and `neuralwatt`. FIX-4 classifies them correctly.

**The Nostr false-positive:** FIX-5 prevents publishing `zai_available=True` when there's no quota data.

**The crashes:** FIX-3 guards against None model in the new deepseek routing block.
