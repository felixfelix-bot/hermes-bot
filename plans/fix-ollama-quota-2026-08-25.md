# Plan: Enable Ollama Cloud Real-Time Price Discovery

**Date:** 2026-08-26
**Status:** EXECUTING
**Severity:** High — ollama_cloud is invisible to the Kalman/pressure system, causing traffic to fall through to paid providers ($19+/day bleed) during 429s

## Problem

The flat router's price discovery treats `ollama_cloud` as infinite free capacity ($0.001/M with zero pressure) because two kill switches are OFF:

- `OLLAMA_EXTRA_USAGE_ENABLED=false` (default) — `_get_ollama_quota_status()` returns all zeros; quota pressure = 0; effective price pinned to $0.001 floor regardless of real usage
- `OLLAMA_QUOTA_PRESSURE_ENABLED=false` (default) — continuous pressure factor (RP-PRICING) not applied

When ollama_cloud 429s (session/weekly quota hit), the router falls through to `routstrd` at $0.53/M — burning $19+/day. The Kalman filter never "sees" ollama's quota depleting because the data source is disabled.

## What the flag does

`OLLAMA_EXTRA_USAGE_ENABLED=true` enables:
1. **`fetch_ollama_usage()`** — fetches real-time usage fractions from `https://ollama.com/api/usage` (per-key, 30s cache, 2s timeout)
2. **`get_quota_status()`** — counts cumulative tokens from `api_calls` table in 5h session and 7d weekly windows
3. **`_get_ollama_quota_status()`** — returns `{regime, session_used_pct, weekly_used_pct, session_tokens, weekly_tokens}` instead of all-zeros
4. The returned `session_used_pct` / `weekly_used_pct` feeds into `quota_pressure_factor()` which multiplies the effective price

`OLLAMA_QUOTA_PRESSURE_ENABLED=true` enables:
- Continuous price-pressure: ollama_cloud's effective price rises **smoothly** as quota depletes (no thresholds, no regime strings)
- Supersedes the binary `extra_usage_multiplier` (EU-R3) and RP-5 throttle for ollama_cloud
- When `session_used_pct` → 100%, effective price → infinity → router proactively reroutes BEFORE 429s

## Checklist

### Phase 1 — Enable the flags
- [ ] Set `OLLAMA_EXTRA_USAGE_ENABLED=true` in zai-proxy environment
- [ ] Set `OLLAMA_QUOTA_PRESSURE_ENABLED=true` in zai-proxy environment
- [ ] Verify httpx installed (needed by fetch_ollama_usage)

### Phase 2 — Restart & verify
- [ ] Clear __pycache__, restart zai-proxy.service
- [ ] Verify gateway started cleanly (no import errors)
- [ ] Verify _get_ollama_quota_status() returns non-zero values (real data from ollama.com)
- [ ] Verify flat_router select_provider reflects pressure on ollama_cloud

### Phase 3 — Smoke test
- [ ] Send test request via zai-proxy
- [ ] Check api_calls shows real quota tracking
- [ ] Monitor 60s for errors

### Phase 4 — Verify pressure propagation
- [ ] Confirm effective cost for ollama_cloud changes with quota state (not pinned at $0.001)
- [ ] Confirm ollama_cloud appears in select_provider BEFORE routstrd when healthy
- [ ] Confirm when quota nears limits, ollama_cloud's effective price rises above $0.001

## Rollback

- Set `OLLAMA_EXTRA_USAGE_ENABLED=false` (and `OLLAMA_QUOTA_PRESSURE_ENABLED=false`) in zai-proxy env
- Restart zai-proxy
- Both flags default to false — code path reverts to legacy binary regime (no pricing impact)