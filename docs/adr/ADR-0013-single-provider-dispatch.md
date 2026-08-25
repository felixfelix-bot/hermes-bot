# ADR-0013: _dispatch_external uses single-provider dispatch, not failover chain

**Date:** 2026-08-25
**Status:** ACCEPTED

## Context

The flat router's `_dispatch_external` called `_try_external_failover(preferred=name)` which iterated ALL external providers (deepinfra, telnyx, ppq, openrouter, routstr, routstrd, neuralwatt). This meant:

- When the flat router picked "neuralwatt" as a candidate, it also tried deepinfra, telnyx, etc.
- When it moved to the next candidate "deepinfra", it re-tried neuralwatt again
- N candidates × 5 active providers = up to 25 API attempts per request
- The flat router's cost-ordered candidate list was meaningless for external providers

## Decision

1. **New `_try_external_single` method** on the Handler: contacts exactly ONE external provider. Shares model translation and response handling with `_try_external_failover`, but does not iterate.

2. **`_dispatch_external` in flat_router.py** now calls `handler._try_external_single(name, body, model, buffer, t0)` instead of `handler._try_external_failover(body, model, buffer, t0, preferred=name)`.

3. **Error handling in `_try_external_single`**:
   - 401/403 → `_mark_key_failure(name, "dead")` → 1h backoff, return False
   - 402 → `_mark_unfunded(name)`, return False
   - 429 → `_mark_key_exhausted(name)`, return False
   - Other HTTPError → log, return False
   - Exception → log, return False

4. **`_try_external_failover`** (old path) is retained unchanged for the rollback path.

## Consequences

- Each flat router candidate tries exactly ONE provider
- The cost-ordered candidate list is now meaningful for external providers
- No more N×5 retry explosion
- Dead keys (401) are marked and excluded from future candidates
- The old failover chain is still available via `.disable_flat_router` rollback