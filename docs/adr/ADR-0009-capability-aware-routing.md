# ADR-0009: Capability-aware routing — PROVIDER_MODELS as source of truth

**Date:** 2026-08-25
**Status:** ACCEPTED

## Context

The flat router's `PROVIDER_MODELS` dict maps provider names to sets of model IDs they can serve. Two problems existed:

1. **z.ai keys listed deepseek models**: `PROVIDER_MODELS["ours"]` and `["friend"]` included `deepseek/deepseek-v4-flash` and `deepseek/deepseek-v4-pro`, but z.ai rejects these with 400 (not in its catalog). This wasted a round-trip on every deepseek request.

2. **Ollama Cloud didn't list deepseek**: `PROVIDER_MODELS["ollama_cloud"]` was missing deepseek models, even though Ollama's API serves `deepseek-v4-flash:0731` and `deepseek-v4-pro:0813` (confirmed working, HTTP 200 in 0.8s). This forced all deepseek traffic to neuralwatt ($1.83/M) instead of ollama_cloud ($0.001/M included).

## Decision

1. **Remove deepseek (and qwen/minimax/mimo) from z.ai keys' PROVIDER_MODELS**. z.ai does not serve these models — they get 400. This is a capability exclusion, NOT a health failure: the key is NOT marked dead, just not a candidate for these models.

2. **Add deepseek to ollama_cloud and ollama_cloud_2 PROVIDER_MODELS** with model name translation: `deepseek/deepseek-v4-flash` → `deepseek-v4-flash:0731`, `deepseek/deepseek-v4-pro` → `deepseek-v4-pro:0813`.

3. **Model name translation** via `_PROVIDER_MODEL_NAMES` is the canonical source for per-provider model ID mapping. `_try_ollama_cloud` now uses it (same pattern as opencode_go).

4. **Broader capability matrix** (auto-discovery from `/models` endpoints, periodic refresh, per-model capability flags like vision/tools/streaming): deferred to future ADR. Current approach is manual curation in `PROVIDER_MODELS`.

## Consequences

- z.ai keys are never tried for deepseek/qwen/minimax/mimo (no wasted round-trips)
- Ollama Cloud serves deepseek at $0.001/M (included) instead of neuralwatt at $1.83/M
- Model name translation handles provider-specific IDs (e.g. `:0731` tag for Ollama)
- Adding new models requires updating `PROVIDER_MODELS` + `_PROVIDER_MODEL_NAMES`