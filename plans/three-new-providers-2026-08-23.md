# Add 3 New Providers to LiveRouter + zai_proxy

**Date:** 2026-08-23
**Status:** COMPLETED

## Problem

Running proxy (PID 2107094, started 03:22) has stale code with a
`_try_ollama_cloud_any` self-referential loop bug (already fixed in the file
but never restarted). Every request crashes with:
`TypeError: Handler._try_ollama_cloud_any() got an unexpected keyword argument 'key_name'`

## Root Cause

Global search-and-replace of `_try_ollama_cloud(` → `_try_ollama_cloud_any(`
also caught the call *inside* the wrapper loop itself. Fixed at line 3699
(changed back to `self._try_ollama_cloud(`). File compiles clean, but the
running process never picked up the fix.

## Code Changes — ALL APPLIED (verified)

- [x] SK-F1. `OLLAMA_CLOUD_API_KEY_2` in `.env`
- [x] SK-F2. Key loading + `OLLAMA_CLOUD_KEY_2` + `_OLLAMA_CLOUD_KEYS`
- [x] SK-F3. `_try_ollama_cloud` generalized + `_try_ollama_cloud_any` wrapper
- [x] SK-F4. Per-key paywall flags (`_OLLAMA_PAYWALL_FLAGS`, key-aware functions)
- [x] SK-F5. `_snapshot_quota`: add `ollama_cloud_2` block (loop over both keys)
- [x] SK-F6. `_snapshot_health`: add `ollama_cloud_2`
- [x] SK-F7. Shadow optimizer: add `ollama_cloud_2` + `opencode_go` + `neuralwatt`
- [x] SK-F8. `live_router.py`: add all three to `_EXTERNAL_PROVIDERS`, `_DEFAULT_CONVERGED_RATES`, `_QUOTA_TOTALS`
- [x] SK-F9. `ollama_quota_tracker.py`: key-aware `get_quota_status(key_name=...)`
- [x] SK-F10. `ollama_extra_usage.py`: per-key billing cache (dict keyed by api_key suffix)
- [x] SK-F11. Update call sites to `_try_ollama_cloud_any` (11 sites + wrapper fixed)
- [x] OG-F1. `.env`: `OPENCODE_GO_API_KEY`
- [x] OG-F2. `_load_external_keys` + `OPENCODE_GO_KEY` + `OPENCODE_GO_BASE`
- [x] OG-F3. `_try_opencode_go` forward method (native glm-5.3, no paywall flag)
- [x] OG-F4. `_PROVIDER_MODEL_NAMES`: opencode_go passthrough
- [x] OG-F5. `EXTERNAL_PROVIDERS` dict: opencode_go entry
- [x] OG-F6. Wire `_try_opencode_go` into forward cascade (2 main cascade points)
- [x] OG-F7. Shadow optimizer: add opencode_go
- [x] OG-F8. Snapshots: add opencode_go + neuralwatt to `_snapshot_quota` + `_snapshot_health`
- [x] OG-F9. `live_router.py`: add opencode_go to registries
- [x] OG-F10. `real_price_tracker.py`: add to `LAST_RESORT_RATES`
- [x] NW-F1. `.env`: `NEURALWATT_API_KEY`
- [x] NW-F2. `_load_external_keys` + `EXTERNAL_PROVIDERS`: neuralwatt
- [x] NW-F3. `_PROVIDER_MODEL_NAMES`: neuralwatt passthrough
- [x] NW-F4. Per-model cost tracking (`NEURALWATT_RATES` dict)
- [x] NW-F5. `live_router.py`: add neuralwatt to registries
- [x] NW-F6. `real_price_tracker.py`: add neuralwatt to `LAST_RESORT_RATES`
- [x] FIX. Wrapper self-referential loop fixed (line 3699: `_try_ollama_cloud_any` → `_try_ollama_cloud`)
- [x] EXTRA. `_KEY_COST_MULTIPLIER` + `_PROVIDER_PRIORITY` updated for new providers
- [x] VERIFY. All 5 files compile clean (`py_compile`)

## Remaining Checklist

- [x] V1. Restart zai-proxy — deploy the fixed code
- [x] V2. Verify `/quota` shows ollama_cloud_2 + opencode_go + neuralwatt
- [x] V3. Verify shadow rates include all new providers with own Kalman
- [x] V4. Live test: send glm-5.2 request → confirm a flat-rate key serves
- [x] V5. espeak-ng notification

## Architecture (what was built)

Each provider gets its own Kalman filter pair (PriceKalman +
ConsumptionKalman) via the LiveRouter `__init__` loop, which creates one
pair per entry in `_DEFAULT_CONVERGED_RATES`. The filters refine from real
`cost_usd` data in the DB, filtered by `key_name` per provider.

| Provider | Kalman | Quota Source | Cost Source |
|----------|--------|-------------|------------|
| ollama_cloud | Pair #1 | `get_quota_status(key_name="ollama_cloud")` | Regime rate × tokens |
| ollama_cloud_2 | Pair #2 | `get_quota_status(key_name="ollama_cloud_2")` | Regime rate × tokens |
| opencode_go | Pair #3 | Local token counter (key_name="opencode_go") | Flat-rate marginal $0.0155 |
| neuralwatt | Pair #4 | inf (pay-per-token) | Per-model NEURALWATT_RATES × tokens |
| ppq | Pair #5 | inf | Per-token |
| openrouter | Pair #6 | inf | Per-token |
| deepinfra | Pair #7 | inf | Per-token |