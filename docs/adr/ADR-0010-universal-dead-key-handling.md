# ADR-0010: Universal dead-key handling — 401/403 marks any provider dead

**Date:** 2026-08-25
**Status:** ACCEPTED

## Context

Previously, only z.ai keys (ours, friend) were marked dead on 401/403 via `_mark_key_dead`. External providers (neuralwatt, deepinfra, etc.) had no dead-key handling — a 401 from neuralwatt was logged but the provider was never marked unhealthy. The flat router kept retrying it on every request, causing the 401-storm (neuralwatt returning 401 hundreds of times per minute).

Additionally, `_try_external_failover` had a bug: non-402 HTTP errors were **re-raised** instead of continuing to the next provider, which aborted the entire failover chain on a 401.

## Decision

1. **401/403 from ANY provider** → `_mark_key_failure(name, "dead")` → 1h backoff. This applies to z.ai keys, ollama_cloud, opencode_go, neuralwatt, deepinfra, telnyx, ppq, openrouter, routstr, routstrd.

2. **New `_try_external_single` method** for the flat router's per-provider dispatch: handles 401/403 by marking dead + returning False (not raising). Handles 402 by marking unfunded. Handles 429 by marking exhausted.

3. **`_try_external_failover`** (old path) also handles 401/403 with `continue` instead of `raise` — so a dead key doesn't abort the chain for the remaining providers.

## Consequences

- A revoked API key (401) is parked for 1h, not retried on every request
- The flat router's candidate list automatically excludes dead keys via `_is_key_healthy()`
- External provider failures are isolated — one dead provider doesn't block others
- Recovery: `_mark_key_healthy(name)` is called on any successful response, clearing the dead state