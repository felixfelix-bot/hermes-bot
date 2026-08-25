# ADR-0008: Backoff policy — dispatch_fail uses 30s flat, not exponential ramp

**Date:** 2026-08-25
**Status:** ACCEPTED

## Context

The flat router's failure handler called `_mark_key_failure(name, "flat_router_dispatch_fail")` which used the same exponential ramp as quota exhaustion (2→4→8→16→32→60→300→900s). A single transient `BrokenPipeError` cascaded to 900s backoff in ~5 minutes. Combined with the death spiral bug (incrementing failures for keys already in backoff), ollama_cloud accumulated 1843 failures and was locked at 900s backoff for 10 hours — despite having 0% quota usage.

## Decision

1. **New error type `"dispatch_fail"`**: uses `_SERVER_ERROR_BACKOFF_SECONDS` (30s flat), same as server errors. Dispatch failures are transient network issues (BrokenPipe, timeout, connection reset), not quota exhaustion.

2. **Death spiral guard** (already committed): only increment failure count when `_is_key_healthy()` returns True (key was actually attempted, not in backoff).

3. **Backoff table** (final):
   - `exhausted` (429/empty): exponential 2→4→8→16→32→60→300→900s
   - `dispatch_fail` (transient network): flat 30s
   - `server` (500/502/503/504): flat 30s
   - `dead` (401/403): flat 1h

## Consequences

- A transient network error costs only 30s of backoff, not 900s
- The death spiral can't recur (guard prevents incrementing for in-backoff keys)
- Keys recover quickly from transient issues
- Only true quota exhaustion (429) escalates to long backoffs