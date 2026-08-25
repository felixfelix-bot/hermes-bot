# ADR-0007: Flat router is the sole routing path

**Date:** 2026-08-25
**Status:** ACCEPTED

## Context

The proxy had three early-exit blocks (Step 1c, 1c-2, 1c-3) that intercepted specific model names before the flat router ran. These bypasses were added "for backward compatibility" but they bypassed the flat router's cost-aware candidate selection, causing:

- Deepseek requests to go directly to neuralwatt ($1.83/M) instead of ollama_cloud ($0.001/M)
- Kimi-k3 to go directly to telnyx ($5.40/M) instead of opencode_go ($0.001/M)
- Ollama-only models to skip the cost-ordered candidate list

The old path (best_key + failover cascade) is retained as a rollback safety net via the `.disable_flat_router` flag file, because it contains model tier routing and compression model selection logic not yet ported to the flat router path.

## Decision

1. **Remove all three early-exit blocks** (Step 1c, 1c-2, 1c-3). All requests now go through the flat router's `select_provider()` → candidate iteration → `_dispatch_to_provider()`.

2. **Keep the old path as dead code** (only reached when `.disable_flat_router` exists). It serves as a rollback safety net and contains model tier routing + compression selection not yet ported.

3. **Keep the pressure FSM enforce hook** and **global spend cap** as circuit breakers before the flat router — these are not routing bypasses.

4. **Move the `messages` presence guard** (422 storm fix) to the top of `_proxy`, before any routing. Model-only probe requests return 400 immediately.

## Consequences

- All models are routed through the flat router's cost-ordered candidate list
- Ollama-only models (kimi-k2.7-code, gpt-oss:120b, etc.) are served by ollama_cloud as the cheapest candidate
- Deepseek models are served by ollama_cloud (after P3 capability fix) or opencode_go, not neuralwatt
- Kimi-k3 is served by opencode_go ($0.001/M) instead of telnyx ($5.40/M)
- Rollback: `touch ~/.hermes/bot/.disable_flat_router` to restore the old path