# ADR-005: Dual Pricing Surface (Internal Routing vs External Sell)

## Status

Accepted

## Date

2026-08-24

## Context

The internal routing prices defined by the 5-tier pricing model (ADR-002 through ADR-004) are intentionally artificial:

- T1 (z.ai quota): as low as $0.000006/M near reset — effectively free
- T3 (opencode_go): $0.001/M — marginal cost is $0
- T4 (ollama_cloud): $0.001/M — included in subscription

These prices are correct for **routing** — they attract traffic to sunk-cost resources, which is the right behavior for minimizing real spend. But they are **wrong for billing external users**. If we sell tokens on routstr at $0.001/M while the real amortized cost is $0.023/M (z.ai: $20/mo ÷ 866M tokens), we lose money on every transaction.

Felix identified this risk on 2026-08-24:

> "There is a risk that we run at a loss for an entire month if we expose these prices on routstr. Expose these prices to our live router which always chooses the cheapest endpoint, but lets make sure that the prices that the live router exposes to real users on routstr are high enough to ensure that we always make a profit when selling the tokens to a third party over routstr."

The internal routing price and the external sell price serve different purposes and must be independent.

## Decision

Maintain **two independent price surfaces**:

**Surface 1 — Internal Routing Price:**
- Function: `compute_effective_price(provider, base_rate, context)` — unchanged from ADR-002.
- Used by: `select_provider()` in `flat_router.py` — picks the cheapest upstream provider.
- Purpose: Attract traffic to sunk-cost providers. Can be below real cost (that's the point — use the prepaid resource).
- Never exposed externally.

**Surface 2 — External Sell Price:**
- Function: `compute_sell_price(provider, model, context)` — new, to be implemented.
- Used by: routstr API — sats/token rate charged to third parties.
- Purpose: Always charge above real cost + profit margin.
- Formula: `sell_price = actual_cost × (1 + margin)`, where `actual_cost` uses real subscription economics (not the artificial routing price).

**Actual cost per tier:**

| Tier | Actual Cost Formula | Example |
|------|---------------------|---------|
| T1 (z.ai quota) | `subscription_cost / monthly_quota_tokens` | $20/mo ÷ 866M = $0.023/M |
| T2 Phase A (NeuralWatt prepaid) | `subscription_cost / typical_monthly_usage` | $100/mo ÷ usage |
| T2 Phase B (NeuralWatt per-token) | `measured_rate` (Kalman) | — |
| T3 (opencode_go) | `subscription_cost / historic_avg_monthly_usage` | $10/mo ÷ 50M = $0.20/M |
| T4 (ollama_cloud) | `subscription_cost / historic_avg_monthly_usage` | — |
| T5 (per-token) | `measured_rate` (from Kalman) | — |

**Profit margin:**
- Default: 20% (`DEFAULT_PROFIT_MARGIN = 0.20`)
- Hard minimum: 10% — `sell_price ≥ actual_cost × 1.1` always enforced, cannot be overridden.
- Per-provider configurable (e.g., opencode_go 30% for unknown capacity risk, per-token 15% for competitive market).

**Safeguards:**
- Unknown cost: `sell_price = max(measured_rate, FALLBACK_RATE) × (1 + margin)` where `FALLBACK_RATE = $0.50/M` (conservative — better to overcharge).
- Zero-cost providers (friend's key): `sell_price = max(FALLBACK_RATE, actual_cost) × (1 + margin)` — don't undercut our own paid providers.
- Monthly profitability check: if `sell_revenue < actual_cost` for any provider → ALERT (not block). Consistent losses → increase margin or remove from routstr.

## Invariants

- Sell price is **always ≥ 1.1× actual cost**. The 10% floor is hardcoded and cannot be overridden by configuration.
- Internal routing price is **never exposed externally**. routstr uses `compute_sell_price()`, never `compute_effective_price()`.
- Monthly profitability check is an **alert, not a block**. It surfaces losses for human review, not automated remediation.
- The two surfaces are independent. Changing the routing price (e.g., time decay on T1) does not change the sell price (which uses actual subscription cost).

## Consequences

**Positive:**
- Always profitable on external sales. The 10% minimum margin floor guarantees we never sell below cost.
- Internal routing optimization is decoupled from external pricing. We can make z.ai effectively free for routing (to use the quota) while selling at $0.028/M (to make profit).
- Monthly profitability alert catches systematic losses early — before they compound over a full billing cycle.

**Negative:**
- Two price systems to maintain. `compute_effective_price()` and `compute_sell_price()` are separate functions with different inputs (routing price uses Kalman + tier formula; sell price uses actual subscription cost).
- Actual cost computation requires subscription metadata (monthly cost, quota amount, historic usage) that may not be available for all providers. Falls back to conservative $0.50/M estimate.
- Per-provider margin configuration adds complexity. Each provider may need a different margin based on risk and market position.

## Notes

- Status: DESIGN ONLY — no code changes yet. `compute_sell_price()` is specified in `capacity-aware-pricing-design.md` §12 (External Sell Pricing).
- The monthly profitability cron job would query `api_calls` for routstr traffic, compute revenue vs cost, and alert on loss.
- Related ADRs: ADR-002 (5-tier model defines the internal pricing surface), ADR-001 (flat router uses Surface 1).