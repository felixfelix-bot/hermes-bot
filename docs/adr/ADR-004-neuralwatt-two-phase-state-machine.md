# ADR-004: NeuralWatt Two-Phase State Machine

## Status

Accepted

## Date

2026-08-24

## Context

NeuralWatt has a $100/mo subscription that includes a fixed kWh allocation. After the included kWh is exhausted, the user can top up at the **same rate** — there is no price increase after exhaustion.

The original T2 pricing model used a **depletion penalty**: `effective = base_rate × (1.0 + depletion_penalty)` where `depletion_penalty = (1.0 - remaining/initial) × 2.0`. This made NeuralWatt progressively more expensive as the balance depleted:

- 100% balance → 1.0× base (normal)
- 50% balance → 1.5× base (depleting)
- 0% balance → 2.0× base (max penalty)

This creates a **vicious circle**: price rises as balance drops → router avoids NeuralWatt → less usage → the provider appears underutilized → further avoidance. The depletion penalty was designed to "preserve" balance, but it penalizes using a sunk-cost resource, which is economically wrong.

The key economic insight: NeuralWatt's included kWh is a **prepaid sunk cost**. The $100/mo is already paid. Marginal cost = $0 while kWh is available. After exhaustion, top-up is at the same per-token rate — no rate increase. There is no economic reason to "preserve" the prepaid kWh; it should be used freely.

Additionally, NeuralWatt's API overcounts usage by ~3.6× (energy-based pricing with 94% cache hit at 5× discount). The correction factor (0.2762 ≈ 1/3.62) is a **measurement correction** — it applies to cost tracking (`_extract_cost`), NOT to the routing price.

## Decision

Replace the depletion penalty with a **two-phase state machine**:

**Phase A (kWh available):** Prepaid sunk cost. Price = $0.001/M (same as T3/T4 floor). No depletion penalty, no gradual price increase. The included kWh is treated as a free resource.

**Phase B (kWh exhausted):** Per-token rate. Price = Kalman-measured $/M (like T5). The top-up rate is the same as the included rate, so this is the real marginal cost.

**Transition trigger:** `remaining_kWh ≤ 0` (from balance bridge API: `_neuralwatt_quota_entry_fn()` returns `is_exhausted = True`).

**No depletion penalty.** Price does not rise as balance depletes during Phase A. The transition from Phase A to Phase B is a discrete state change, not a gradual ramp.

**Correction factor (0.2762):** Applied in `_extract_cost()` for accurate cost tracking (what we record in `api_calls.cost_usd` and feed to the Kalman). NOT applied as a multiplier on the routing price — the router sees the real marginal cost ($0.001/M in Phase A, measured rate in Phase B).

## Invariants

- Price **never rises** as balance depletes during Phase A. This is the vicious circle prevention invariant.
- The Phase A → Phase B transition is a discrete jump, not a gradual ramp. Price goes from $0.001/M to the measured per-token rate.
- The correction factor (0.2762) is measurement-only. It corrects the cost we record, not the price the router uses for selection.
- If the balance bridge is disabled or returns no data, NeuralWatt is treated conservatively (Phase B, per-token rate) — we don't route to it at sunk-cost prices if we can't verify the balance.

## Consequences

**Positive:**
- NeuralWatt is used freely while prepaid kWh is available. No artificial conservation of a sunk-cost resource.
- Eliminates the vicious circle. Using NeuralWatt doesn't make it more expensive.
- Correctly models the economics: Phase A = prepaid (marginal $0), Phase B = per-token (marginal = measured rate). Top-up at same rate = no rate increase.
- The correction factor ensures accurate cost tracking without distorting routing decisions.

**Negative:**
- Doesn't "preserve" NeuralWatt balance for high-value work. If Felix wants to save it for specific use cases, that requires a future "preference weight" on top of the base price — not a pricing penalty.
- Phase B price depends on Kalman convergence. If NeuralWatt has never been used in Phase B, the Kalman has no signal and falls back to seed rates.
- Open question: does unused kWh carry over monthly? If NO → a monthly time decay (like T1 but 30-day cycle) should be added to Phase A. Default assumption: does NOT carry over.

## Notes

- Status: DESIGN ONLY. The depletion penalty is still in live code (commit `db88e0f`). The two-phase state machine is designed but not implemented (design commit `7fb481f`).
- The NeuralWatt correction factor fix in `_extract_cost()` is live (commit `dcb648c`).
- Related ADRs: ADR-002 (5-tier model, T2 definition), ADR-003 (sunk cost principle).