# ADR-001: Flat Router with Kalman Price Discovery

## Status

Accepted

## Date

2026-08-24

## Context

The bot's original routing architecture used a two-tier hierarchy: z.ai as the primary provider, with all other providers serving as failover targets. This design caused two critical problems:

1. **Cost inflation** — When z.ai was unavailable or unhealthy, all traffic fell to a single failover provider (typically NeuralWatt at ~$2.21/M), driving up costs. The cost-escalation alert fired at $33.71/h, inflated 15.7× over the real ~$0.43/h burn rate.

2. **Single point of failure** — With z.ai as the sole primary, any quota exhaustion, API outage, or key health degradation cascaded immediately to the failover tier with no further redundancy.

3. **Backwards routing** — The early-exit path for `deepseek/*` models bypassed the flat router entirely, sending traffic to NeuralWatt ($1.43/M) instead of opencode_go ($0 marginal cost), because `_try_opencode_go()` sent the raw model name `deepseek/deepseek-v4-flash` instead of the bare `deepseek-v4-flash` that opencode.ai expects.

The fundamental issue was that the tiered architecture prevented real price comparison. Providers were ranked by preference order, not by cost.

## Decision

Replace the two-tier hierarchy with a **flat router** where all 12 providers participate equally.

- `select_provider()` filters candidates by model match and health, then sorts by `effective_cost` ascending.
- Each provider gets its own `PriceKalman` (measures real $/M from traffic) and `ConsumptionKalman` (tracks usage rate).
- No tiers, no preference order, no hardcoded primary/failover distinction.
- Failover is implicit: the candidate list sorted by cost IS the failover chain. If the cheapest provider fails (429, 401, timeout), the router tries the next cheapest.

All 12 providers: `ours` (z.ai), `friend` (z.ai), `neuralwatt`, `opencode_go`, `ollama_cloud`, `ollama_cloud_2`, `routstr`, `routstrd`, `deepinfra`, `ppq`, `telnyx`, `openrouter`.

## Invariants

- All providers are equal in the candidate list. No provider is "primary."
- Kalman filters measure real $/M from actual traffic — prices are discovered, not configured.
- Failover = trying the next cheapest provider in the sorted candidate list.
- `select_provider()` core sort logic (cheapest first) is never overridden by tier-specific adjustments — those adjustments change WHAT the cost IS, not the sort order.

## Consequences

**Positive:**
- No single point of failure. If any provider goes down, traffic flows to the next cheapest.
- Real price discovery via Kalman smoothing. Providers compete on actual measured cost.
- Adding a new provider is trivial: add it to `PROVIDER_MODELS`, `PROVIDER_KEYS`, and the pricing tier dict. No failover chain to update.

**Negative:**
- More complex health tracking — 12 providers each need key health monitoring, Kalman state, and tier-specific pricing logic.
- Price discovery requires traffic. A provider that has never been used has no Kalman signal and falls back to seed/estimate rates.
- The flat router requires model translation per-provider (`_PROVIDER_MODEL_NAMES`) — each provider expects different model name formats.

## Notes

- Implementation: commit `a6b086e` — flat router live with 12 providers.
- The early-exit path for `deepseek/*` models (line ~4939) was fixed in commit `a346900` to use `_PROVIDER_MODEL_NAMES` for model translation, allowing opencode_go to handle deepseek models correctly.
- Related ADRs: ADR-002 (tier-specific pricing on top of the flat router), ADR-003 (sunk cost time decay), ADR-004 (NeuralWatt two-phase).