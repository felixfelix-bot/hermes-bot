# Plan: Fix Ollama Cloud Routing Death Spiral + Stale External Keys

**Date:** 2026-08-25
**Status:** READY FOR IMPLEMENTATION

## Root Cause Analysis

### Problem 1: Ollama Cloud death spiral (PRIMARY)

The flat router (`flat_router.py` line 5286) calls `_mark_key_failure(_cand.name, "flat_router_dispatch_fail")` every time a dispatch function returns False — **including when the key is already in backoff** and was never actually tried.

The flow:
1. First transient failure → `_mark_key_failure("ollama_cloud", "flat_router_dispatch_fail")` → backoff set
2. Next request: `_is_key_healthy("ollama_cloud")` → False (in backoff) → `_try_ollama_cloud` returns False immediately (line 4197-4198) without making any HTTP request
3. Flat router sees False → calls `_mark_key_failure` AGAIN → increments failure count
4. Repeat 1843 times → backoff maxed at 900s (15 min) → key never recovers

**Evidence:**
- `key_health` table: ollama_cloud has **1843 consecutive failures**, all type `flat_router_dispatch_fail`
- ollama_cloud_2 has **1843 failures** (same count — same death spiral)
- friend has **1678 failures**, opencode_go has **2843 failures**
- The OLD ollama key is actually VALID (curl returns HTTP 200 on `https://ollama.com/v1/models`)
- Last successful ollama_cloud API call was 584 min ago (~10 hours)
- All failures are `flat_router_dispatch_fail` — none are `exhausted` (429) or `dead` (401/403), meaning the API was never contacted after the first failure

### Problem 2: Stale neuralwatt key

- neuralwatt API key (`sk-d843a...`) was returning HTTP 200 until 9 min ago
- Now returning HTTP 401 "API key is inactive"
- The key may have been deactivated/expired independently

### Problem 3: Stale oxalpha key

- OPENROUTER_OXALPHA_KEY returning HTTP 401 consistently

### Problem 4: New ollama key not stored

- User provided new key: `sk-76de5607b55bab33cd3fa205657661530a39be37cdeb487d4bf7d49079472bf0`
- Old key #1 (`0b57012a...`) is still valid and should be kept
- Old key #2 (`791f61f0...`) status unknown — should be tested

## Changes

### F1: Fix the flat router death spiral

In `zai_proxy.py` flat router path (line ~5283-5289), don't call `_mark_key_failure` when the dispatch returned False because the key was in backoff. Instead, only mark as failed when the key was actually healthy and the dispatch was attempted but failed.

**Option A (preferred):** Check `if _is_key_healthy(_cand.name)` before calling `_mark_key_failure`. If the key is already unhealthy (in backoff), skip the failure increment — it wasn't actually tried.

**Option B:** Have `_try_ollama_cloud` and `_try_external_failover` return a tri-state (success/failed-not-tried/failed-tried) instead of bool. More complex but more precise.

### F2: Reset key health for all affected providers

Clear the `key_health` table or reset the failure counts for ollama_cloud, ollama_cloud_2, friend, ours, opencode_go. This removes the 1843+ failure counts and lets the keys start fresh.

### F3: Store the new ollama key

Update `~/.hermes/profiles/manager/.env`:
- `OLLAMA_CLOUD_API_KEY_2` → new key `sk-76de5607b55bab33cd3fa205657661530a39be37cdeb487d4bf7d49079472bf0`

Or replace key #1 with the new key and move the old key #1 to key #2 slot (since old key #1 is confirmed valid).

### F4: Clear stale paywall flag files

Remove `~/.hermes/bot/.ollama_exhausted_until` and `~/.hermes/bot/.ollama_exhausted_until_2` if they exist (they may be expired but should be cleared for a clean slate).

### F5: Restart zai-proxy and verify

## Checklist

- [ ] F1: Fix flat router death spiral — don't increment failure count for keys in backoff
- [ ] F2: Reset key_health table (clear 1843+ failure counts)
- [ ] F3: Store new ollama key in manager/.env
- [ ] F4: Clear stale paywall flag files
- [ ] F5: Restart zai-proxy
- [ ] F6: Verify ollama_cloud is receiving traffic (check DB for status=200 entries)
- [ ] F7: Verify neuralwatt key status (may need user to rotate)
- [ ] F8: espeak-ng notification
