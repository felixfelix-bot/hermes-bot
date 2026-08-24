# ADR-003: Sunk Cost Time Decay for Quota Providers

## Status

Accepted

## Date

2026-08-24

## Context

z.ai quota providers (T1) have a weekly quota that is prepaid — use it or lose it. Unused quota at reset time is wasted.

The original T1 formula multiplied `base_rate × time_decay × quota_health`. At the start of the week (7 days to reset, 0% used), `time_decay = days_to_reset / 7 = 1.0`, producing the **full base rate**. This made z.ai the **most expensive** provider when quota was freshest — exactly backwards.

The correct economic reasoning: z.ai quota is a sunk cost (already paid for). Marginal cost = $0 while quota is available. The price should be lowest when the quota is freshest (plenty of time to use it) and drop as reset approaches (use-it-or-lose-it urgency).

The original formula also included `quota_health = (1 - used_pct/100)`, which penalized providers for using their quota — a conservation penalty that makes no sense for a use-it-or-lose-it resource.

## Decision

Replace the T1 formula with a **sunk cost + time decay** model:

```
effective_price = $0.001 × max(0.0001, days_to_reset / 7)
```

Key properties:
- The base is `$0.001/M` (the MIN_EFFECTIVE_PRICE floor, representing sunk cost — effectively $0 but non-zero to avoid always-wins).
- Time decay multiplies this floor: at 7 days to reset, `effective = $0.001 × 1.0 = $0.001/M`. At 1 day, `effective = $0.001 × 0.143 = $0.000143/M`. At 1 hour, `effective = $0.001 × 0.006 = $0.000006/M`.
- **No conservation penalty.** There is no `quota_health` multiplier. Using quota does not increase price.
- **No quota_health gate in pricing.** When quota is 100% used, the provider is marked unavailable (excluded from candidate list by health gate), not priced at $0.

The time decay applies to the $0.001 floor (sunk cost), NOT to the base_rate. The Kalman's `base_rate` estimate is unaffected — it continues to learn the true $/M from traffic.

## Invariants

- Quota available → price near $0 (attracts traffic, uses the sunk-cost resource).
- Quota exhausted → provider unavailable → failover to next cheapest. No vicious circle.
- Price monotonically decreases as reset approaches. Never increases.
- No conservation penalty. Using quota never makes the provider more expensive.
- The $0.001 floor ensures z.ai doesn't trivially always-win over other $0.001 floor providers (T3/T4) — they're equal at the start of the week, and z.ai becomes cheaper as reset nears.

## Consequences

**Positive:**
- Quota is used before it's wasted. As reset approaches, z.ai becomes the cheapest provider and attracts traffic.
- No vicious circle. Using quota doesn't increase price.
- Simple open-loop formula. No controller, no tuning, no stability analysis needed.
- Correct economic modeling: sunk cost = $0 marginal cost, time decay = urgency to use before waste.

**Negative:**
- Doesn't adapt to actual burn rate. If we burn through quota in 2 days, the formula doesn't slow down. If we burn slowly, it doesn't speed up. It's a linear ramp, not a closed-loop controller.
- The LQG controller (design in `capacity-aware-pricing-design.md`) would be more accurate — it observes actual burn rate and adjusts price to ensure full exhaustion just before reset. This is a future enhancement, T1-only.

## Notes

- Implementation: commit `7220cd3` (T1 sunk cost + time decay, replacing the backwards formula).
- The `days_to_reset` value comes from `quota_cache` (weekly window `resets_at` field), populated by the background `_refresh_loop()` every 5 minutes.
- The LQG controller design is in `capacity-aware-pricing-design.md` §2-3. It uses `ConsumptionKalman` for state estimation and a heuristic LQR for the control law. Phase 1-2 enhancement after the current formula proves stable.
- Related ADRs: ADR-001 (flat router), ADR-002 (5-tier model).