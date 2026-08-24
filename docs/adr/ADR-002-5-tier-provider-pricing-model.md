# ADR-002: 5-Tier Provider Pricing Model

## Status

Accepted

## Date

2026-08-24

## Context

The flat router (ADR-001) treats all providers equally and sorts by `effective_cost`. But providers have fundamentally different economic models:

- **z.ai** has a weekly quota — prepaid, use-it-or-lose-it. Marginal cost = $0 while quota available, infinite when exhausted.
- **NeuralWatt** has a monthly kWh allocation included in a $100/mo subscription. After exhaustion, can top up at the SAME rate (no price increase). Two distinct economic phases.
- **opencode_go** is a $10/mo flat-rate plan. Marginal cost = $0 regardless of usage volume.
- **ollama_cloud** is included in a subscription. Marginal cost = $0.
- **routstr, openrouter, deepinfra, ppq, telnyx** are pure pay-per-token. Each request costs real money. No capacity, no reset.

A single pricing formula cannot correctly model all these cases. Applying the per-token Kalman price to a sunk-cost provider creates the **vicious circle**: underused provider → fewer tokens → measured $/M rises → router avoids it → even fewer tokens → death spiral.

Conversely, treating a per-token provider as $0 (like a flat-rate provider) would route all traffic to it and cause unlimited real spend.

## Decision

Classify each provider by its economic model into 5 tiers. Each tier has its own `compute_effective_price()` formula:

| Tier | Type | Providers | Economic Model | Pricing Approach |
|------|------|-----------|----------------|-------------------|
| T1 | Quota | z.ai ours, z.ai friend | Weekly quota, use-it-or-lose-it, resets weekly | Sunk cost + time decay |
| T2 | Prepaid + per-token | NeuralWatt | Monthly kWh included, top up at same rate | Two-phase state machine (ADR-004) |
| T3 | Flat-rate | opencode_go | $10/mo, unlimited/unknown capacity | $0.001/M static floor |
| T4 | Included | ollama_cloud, ollama_cloud_2 | Included in subscription | $0.001/M static floor |
| T5 | Per-token | routstr, routstrd, deepinfra, ppq, telnyx, openrouter | Pure pay-per-token | Kalman observer (measures real $/M) |

The `PROVIDER_TIER` dict maps each provider name to its tier. `compute_effective_price()` dispatches to the tier-specific formula.

## Invariants

- Sunk-cost providers (T1, T2 Phase A, T3, T4) are **never penalized for usage**. Using a prepaid resource does not increase its price.
- Per-token providers (T5, T2 Phase B) are **never given an artificial discount**. Their price reflects real marginal cost.
- The $0.001/M floor prevents the "always wins" edge case where a $0 provider gets ALL traffic and gets overwhelmed.
- The `select_provider()` sort is unchanged — tiers change WHAT the cost IS, not HOW candidates are sorted.

## Consequences

**Positive:**
- Correct economic modeling per provider type. Sunk-cost resources are cheap (attract traffic), per-token resources reflect real cost.
- Eliminates the vicious circle for sunk-cost providers. Their price is based on economic reality, not on how much they've been used.
- Adding a new provider requires answering 5 onboarding questions (fixed quota? carryover? exhaustion behavior? price increase? subscription cost?) that map directly to a tier.

**Negative:**
- More configuration needed per provider. Each provider needs tier classification and tier-specific parameters (quota reset time, balance bridge, measured rate correction).
- Tier-specific formulas add code complexity. Five code paths instead of one.
- Tier misclassification causes routing errors. If a per-token provider is classified as flat, it gets $0.001/M and attracts all traffic — unlimited real spend.

## Notes

- Implementation: T1/T3/T4/T5 live in commit `db88e0f` (with T1 correction in `7220cd3`). T2 two-phase is design only (ADR-004).
- The `compute_effective_price()` function and `PROVIDER_TIER` dict live in `flat_router.py`.
- Provider onboarding Step 2.5 (skill commit `fd61800`) collects tier classification for new providers.
- Related ADRs: ADR-001 (flat router), ADR-003 (T1 time decay), ADR-004 (T2 two-phase), ADR-005 (dual pricing surface).