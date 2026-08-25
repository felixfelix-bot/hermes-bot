# Plan: Fix Hermes Frequent Context Compression

**Date:** 2026-08-25
**Status:** DONE
**Severity:** High — compression fires every ~1-4 hours instead of ~every few days

## Root Cause (Verified)

Two compounding bugs cause Hermes to think `glm-5.2` has a 202K context window instead of its actual 1M:

1. **`_query_ollama_api_show` crashes** with `ModuleNotFoundError: No module named 'httpx'` because httpx isn't installed in the Hermes venv. This aborts `get_model_context_length()` before it can reach the hardcoded catalog fallback.

2. **Silent exception swallowing** in the gateway's broad `except Exception: pass` means the crash is invisible — the context_length resolves to the broad `glm` fallback (202,752) instead of the exact `glm-5.2` entry (1,048,576).

**Result:** `threshold_tokens = 202,752 × 0.70 = 141,926 ≈ 140,000` — matching the observed compression threshold. Compression fires at ~140K instead of ~734K.

## Checklist

### Phase 1 — Immediate fix: pin context_length in config
- [x] Add `model.context_length: 1048576` to `~/.hermes/config.yaml`
- [x] Add same override to `~/.hermes/profiles/manager/config.yaml` if it has a model section

### Phase 2 — Fix the crash in model_metadata.py
- [x] Wrap `import httpx` in `_query_ollama_api_show` with try/except ModuleNotFoundError
- [x] Wrap `import httpx` in `_query_local_context_length` with try/except ModuleNotFoundError
- [x] Verify `get_model_context_length('glm-5.2', base_url='http://localhost:9099', provider='zai')` returns 1048576

### Phase 3 — Restart Hermes gateway
- [x] Restart `hermes-gateway.service`
- [x] Verify compression threshold is now ~734K in logs
- [x] Smoke test: send a message via Signal, confirm response

### Phase 4 — Add context_window to zai_proxy /v1/models (defense in depth)
- [x] Add `context_window` field to model entries in zai_proxy `/v1/models` response
- [x] Restart zai-proxy
- [x] Verify `/v1/models` returns context_window for glm-5.2

### Phase 5 — Install httpx (proper fix)
- [x] Install httpx in the Hermes venv
- [x] Re-verify all probes succeed without the config override

## Verification

- [x] `get_model_context_length('glm-5.2', ...)` returns 1,048,576 without crashing
- [x] Compression threshold is ~734K (70% of 1M), not ~140K
- [x] No compression fires during normal conversation turns
- [x] Hermes responds to Signal messages after restart