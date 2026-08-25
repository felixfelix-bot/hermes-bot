# ADR-0011: Per-model pressure pricing for flat-rate and included providers

**Date:** 2026-08-25
**Status:** ACCEPTED

## Context

Flat-rate (T3: opencode_go) and included (T4: ollama_cloud) providers had a flat $0.001/M effective price regardless of quota pressure or model mix. This meant the router couldn't distinguish "glm-5.2 is burning all of opencode_go's allowance" from "kimi-k3 is burning all of ollama_cloud's session quota" — both were $0.001/M.

The user wants per-model pressure: if GLM-5.2 burn is high on opencode_go, route glm-5.2 to ollama_cloud instead. If kimi-k3 burn is high on ollama_cloud, route kimi-k3 to opencode_go instead.

## Decision

1. **Parse OpenCode Go `allowance_remaining_usd`** from response bodies in `_try_opencode_go`. Store in `_opencode_go_allowance` cache. Quota fraction = remaining / $10 (initial allowance).

2. **Per-(provider, model) burn share** from `api_calls` table: `_compute_model_burn_share(provider, model)` queries token counts per model per provider in the last 1h. Cached for 60s.

3. **New effective price formula for T3/T4**:
   ```
   effective = MIN_EFFECTIVE_PRICE × (1 + scarcity + burn_share × scarcity × BURN_PREMIUM_FACTOR)
   ```
   where:
   - `scarcity` = 1 - quota_fraction (0 = full quota, 1 = depleted)
   - `burn_share` = model's share of provider's total token burn (0 to 1)
   - `BURN_PREMIUM_FACTOR` = 2.0 (tunable)

   When scarcity is 0 (full quota), effective = MIN_EFFECTIVE_PRICE (no pressure).
   When scarcity is high AND burn_share is high, effective rises significantly.

4. **Emergent behavior**: when opencode_go's allowance depletes mostly from glm-5.2 burn, glm-5.2's Go price rises above ollama_cloud's floor → glm-5.2 migrates to ollama_cloud. When ollama's session fills mostly from kimi-k3, kimi-k3's ollama price rises above Go's floor → kimi-k3 migrates to Go.

## Consequences

- Flat-rate/included providers now have model-specific pricing under quota pressure
- High-burn models are pushed off scarce providers first
- The router automatically balances load across flat-rate providers based on per-model burn patterns
- `BURN_PREMIUM_FACTOR` is a single tunable constant (starting at 2.0)