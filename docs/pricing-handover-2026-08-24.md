# API Cost Tracking & Pricing Model — Handover Document

**Date:** 2026-08-24
**Author:** Felix + Hermes Manager
**Status:** T2/T5 design complete, T1/T3/T4/T5 implemented, T2 implementation pending

---

## 1. The Problem

### 1.1 Inflated Cost Reporting

The cost-escalation alert fired at $33.71/h burn rate. Investigation revealed this was inflated **15.7x** — real cost was ~$0.43/h. Three root causes:

1. **NeuralWatt 3.6x overcounting not applied in _extract_cost()**
   - The 0.2762 correction factor was loaded in code and worked in `_estimate_cost_usd()`
   - But `_extract_cost()` (which records actual spend to DB) used UNCORRECTED rates
   - NeuralWatt appeared to cost $1.27/M instead of real ~$0.35/M
   - The Kalman filter learned the inflated rate, compounding the error

2. **opencode_go recorded at $0.43/M but real cost is $0**
   - $10/mo flat-rate plan — marginal cost is $0
   - The $0.43/M "estimated equivalent" was for dashboard spend tracking, not actual cash burn
   - $13/h of "spend" wasn't real money

3. **ollama_cloud same issue** — included in subscription, marginal $0, but recorded $0.29/h

### 1.2 Wrong Routing — Traffic to Expensive Providers

The flat router was sending deepseek-v4-flash traffic to NeuralWatt ($1.43/M) instead of opencode_go ($0 marginal). Root cause: a two-part bug:

- Line ~4939: early-exit path fires for ALL `deepseek/*` models BEFORE the flat router sees them
- `_try_opencode_go()` sends raw model name `deepseek/deepseek-v4-flash` but opencode.ai expects bare `deepseek-v4-flash`
- opencode_go rejects it → falls through to NeuralWatt

### 1.3 Invisible Burn — NULL Cost Tracking

The invisible burn detector caught routstr burning 1.25M tokens with 100% NULL cost_usd. Root cause: `_extract_cost()` had specific branches for ollama_cloud, opencode_go, telnyx, neuralwatt — but NOT for routstr, routstrd, deepinfra, ppq, openrouter. These fell through to `return (None, None)`.

Additional invisible burn found:
- ppq: 22 calls, 100% NULL (response format changed)
- opencode_go: 33% NULL (streaming SSE passes total_tokens=0)
- ollama_cloud_2: 99% NULL (same streaming issue + branch not extended)

### 1.4 The Vicious Circle Problem

The original pricing model calculated base_price = subscription_cost / actual_usage. This creates a death spiral:

- Underused provider → fewer tokens → base_price rises → router avoids it → even fewer tokens → price rises more
- A provider costing $10/mo that serves 5M tokens shows $2.00/M → router avoids it → serves 2M → shows $5.00/M → avoided more

### 1.5 The Backwards T1 Formula

The first T1 (z.ai quota) formula multiplied base_rate × time_decay. At the START of the week (7 days to reset, 0% used), this produced the FULL base rate — making z.ai the MOST expensive provider when quota was freshest. This is backwards: z.ai quota is a sunk cost (already paid for) and should be CHEAPEST when available.

---

## 2. Our Approach

### 2.1 5-Tier Provider Classification

Each provider is classified by its economic model, not just its API:

| Tier | Providers | Economic Model | Pricing Approach |
|------|-----------|----------------|-------------------|
| T1 (quota) | z.ai ours, z.ai friend | Weekly quota, use-it-or-lose-it, resets Aug 27 | Sunk cost + time decay, LQG controller (future) |
| T2 (prepaid+per-token) | NeuralWatt | Monthly kWh included, top up at same rate | Two-phase state machine |
| T3 (flat-rate) | opencode_go | $10/mo, unlimited/unknown capacity | $0.001/M static floor |
| T4 (included) | ollama_cloud, ollama_cloud_2 | Included in subscription | $0.001/M static floor |
| T5 (per-token) | routstr, routstrd, deepinfra, ppq, telnyx, openrouter | Pure pay-per-token | Kalman observer (measures real $/M) |

### 2.2 T1 — Sunk Cost with Time Decay (Quota Providers)

z.ai quota is a sunk cost — already paid for. Marginal cost = $0 when available.

**Formula:** `effective = $0.001 × max(0.0001, days_to_reset / 7)`

| Time to reset | Effective price | vs opencode_go |
|---|---|---|
| Start of week (7 days) | $0.001/M | Equal — router spreads load |
| 3 days | $0.00043/M | z.ai preferred (use it) |
| 1 day | $0.00014/M | z.ai strongly preferred |
| 1 hour | $0.000006/M | Aggressively burn remaining quota |
| 100% used | unavailable | Failover to next cheapest |

Key principle: the time decay applies to the $0.001 floor (sunk cost), NOT to the base_rate. No conservation penalty — if quota runs out, failover handles it.

**Future enhancement (LQG controller):** Replace open-loop time decay with a closed-loop controller that observes actual burn rate, predicts end-of-period usage, and adjusts price to ensure quota is fully exhausted just before reset. This is the ONLY tier where a controller makes sense — it has a hard capacity and known reset time.

### 2.3 T2 — Two-Phase State Machine (NeuralWatt)

NeuralWatt is NOT a simple balance provider. It has two economic phases:

**Phase A (included kWh available):** PREPAID. The $100/mo includes a fixed kWh allocation. This is a sunk cost — marginal cost = $0. Price = $0.001/M (same as T3/T4). NO depletion penalty (the old model penalized using a sunk-cost resource, creating the vicious circle).

**Phase B (kWh exhausted):** PAY PER TOKEN at the SAME rate. No rate increase — can top up at the same price. Marginal cost = Kalman-measured $/M (like T5). Price = measured rate.

**Transition trigger:** remaining_kWh ≤ 0 (from balance bridge API).

**Open question:** Does unused kWh carry over monthly? If NO → add monthly time decay (like T1 but 30-day cycle). If YES → no urgency, $0.001 floor always. Default assumption: does NOT carry over.

**Correction factor (0.2762):** This is a MEASUREMENT correction (their API overcounts usage 3.6x). It applies to COST TRACKING (what we record as spend in _extract_cost), NOT to the ROUTING PRICE. The router sees the real marginal cost.

### 2.4 T3/T4 — Static Floor (Flat-Rate / Included)

opencode_go ($10/mo) and ollama_cloud (included) have marginal cost = $0. Effective price = $0.001/M always (tiny non-zero to avoid always-wins edge case). Health drops on 429/rate-limit. No controller, no time decay, no balance tracking needed.

### 2.5 T5 — Kalman Observer (Per-Token)

routstr, openrouter, deepinfra, ppq, telnyx have no capacity, no reset, no balance. Price = measured actual cost per token. The Kalman filter OBSERVES (not controls) — tracks real $/M from traffic. No optimization needed.

### 2.6 Catch-All Cost Extraction Fallback

Any provider without a specific `_extract_cost()` branch now gets cost estimated from `_rpt_rate(provider) × tokens`. This prevents future invisible burn for new providers.

### 2.7 Provider Onboarding Configuration

The "adding-api-key-to-live-router" skill now includes Step 2.5 — pricing model configuration. Every new provider gets asked:
1. Fixed quota or capacity? (weekly/monthly/unlimited)
2. Does unused capacity carry over?
3. What happens when exhausted? (unavailable/pay-per-token/rate-limited)
4. Price increase after exhaustion? (yes/no)
5. Subscription cost? (for profitability tracking)
6. Measured rate from API? (for cost tracking correction)

Answers map to T1-T5 tiers automatically.

### 2.8 Profitability Tracking (Decoupled from Pricing)

A separate `subscription_profitability` table tracks actual usage vs subscription cost for monthly renewal decisions. Value score = composite (tasks completed × latency × reliability × quality). This is a REPORTING metric, NOT a pricing input.

---

## 3. Tradeoffs

### 3.1 Time Decay vs LQG Controller (T1)

**Current (time decay):** Simple open-loop formula. Price drops linearly as reset approaches. Doesn't adapt to actual burn rate — if we burn fast early, it doesn't slow down; if we burn slow, it doesn't speed up.

**Future (LQG controller):** Closed-loop. Observes actual burn rate, predicts end-of-period usage, adjusts price to ensure full exhaustion. More accurate, but more complex — needs ConsumptionKalman (already exists) + LQR control law (needs implementation).

**Tradeoff:** The simple time decay is "good enough" for now. It ensures unused quota gets cheaper as reset approaches, which is the primary goal. The LQG controller is an enhancement that would fine-tune the burn rate. It's T1-only — other tiers don't need it.

### 3.2 Two-Phase Model vs Depletion Penalty (T2)

**Old (depletion penalty):** Price rises as balance drops. Intended to preserve remaining balance. But creates vicious circle: price rises → router avoids → less usage → worse.

**New (two-phase):** $0.001/M while prepaid kWh available, then switch to per-token rate. No gradual penalty — just a state transition. Simpler, avoids the vicious circle, correctly models the economics (top-up at same rate = no rate increase).

**Tradeoff:** The two-phase model doesn't "preserve" NeuralWatt for high-value work — it treats the prepaid kWh as a sunk cost to be used freely. This is correct per Felix's economics: the kWh is already paid for, so marginal cost = $0. If Felix wants to preserve it, that's a future "preference weight" on top of the base price.

### 3.3 $0.001 Floor vs True $0 (T3/T4)

opencode_go and ollama_cloud have true marginal cost = $0. We price at $0.001/M (tiny non-zero) to avoid the "always wins" edge case where the router sends ALL traffic to one provider and overwhelms it. The $0.001 is effectively $0 for routing purposes but allows health-based failover.

**Tradeoff:** If opencode_go gets rate-limited (429), its health drops and the router picks the next provider. Without the non-zero floor, the router might keep retrying the rate-limited provider because it's still $0.

### 3.4 Kalman Observer vs Controller (T5)

Per-token providers use the Kalman as a passive observer — it measures $/M from real traffic. It doesn't try to steer consumption because there's no capacity to exhaust.

**Tradeoff:** The Kalman can only learn from actual traffic. If a provider has never been used (like deepinfra with 0 calls), the Kalman has no signal and falls back to a last-resort estimate. The routstr_probe.py daily probe is supposed to seed real measurements, but it's currently broken (401/405 errors).

### 3.5 Catch-All Fallback vs Provider-Specific Branches

The catch-all fallback uses `_rpt_rate(provider) × tokens` for any provider without a specific `_extract_cost()` branch. This prevents NULL costs but is imprecise — it uses the Kalman's measured or estimated rate, not the provider's actual billing API.

**Tradeoff:** Provider-specific branches (like NeuralWatt's per-model rates + correction, or Telnyx's cache-aware calculation) are more accurate. But they require manual implementation for each provider. The catch-all is a safety net — better to have an imprecise cost than NULL (invisible burn).

---

## 4. What's Implemented vs What's Design Only

| Component | Status | Commit |
|-----------|--------|--------|
| Flat router (12 providers, select_provider) | LIVE | a6b086e |
| T1 sunk cost + time decay | LIVE | 7220cd3 |
| T2 depletion penalty (WRONG — needs replacement) | LIVE | db88e0f |
| T3/T4 $0.001 floor | LIVE | db88e0f |
| T5 Kalman observer | LIVE | db88e0f |
| NeuralWatt correction in _extract_cost | LIVE | dcb648c |
| deepseek model translation in _try_opencode_go | LIVE | a346900 |
| Catch-all cost extraction fallback | LIVE | a41ed60 |
| T1 LQG controller | DESIGN ONLY | — |
| T2 two-phase state machine | DESIGN ONLY | 7fb481f |
| Profitability tracking table | DESIGN ONLY | — |
| Provider onboarding Step 2.5 (pricing questions) | LIVE (skill) | fd61800 |

---

## 5. Known Issues Remaining

1. **Streaming SSE total_tokens=0** — streaming responses pass total_tokens=0 to _extract_cost, causing 33% NULL for opencode_go and 99% NULL for ollama_cloud_2. Root cause of remaining invisible burn.

2. **routstr_probe.py broken** — daily cost probe fails: routstr returns 401 (stale key), routstrd returns 405 (daemon API changed). No measured rates for the Kalman to learn from.

3. **ppq response format changed** — cost_extraction.py has a ppq entry but it stopped working. 22 calls, 100% NULL.

4. **T2 depletion penalty still in live code** — the two-phase state machine is designed but not implemented. Current behavior penalizes NeuralWatt usage as balance drops (wrong).

5. **z.ai quota locked** — resets Aug 27. T1 formula is correct but inactive until quota resets.

6. **opencode_go key health** — was 401ing earlier today. After subscription activation, key started working. But health recovery may not be complete — check key_health table.

---

## 6. Design Documents

- `~/.hermes/profiles/manager/state/time-aware-pricing-design.md` — original 5-tier pricing design
- `~/.hermes/profiles/manager/state/capacity-aware-pricing-design.md` — LQG controller + profitability tracking design (1208 lines)
- `~/.hermes/profiles/manager/state/invisible-burn-analysis.md` — routstr invisible burn analysis (24KB)
- `~/.hermes/profiles/manager/skills/devops/adding-api-key-to-live-router/SKILL.md` — 11-step + Step 2.5 onboarding skill

---

## 7. Recommended Next Steps

1. **Implement T2 two-phase state machine** — replace depletion penalty with prepaid kWh → per-token transition. Highest impact: NeuralWatt becomes cheap while kWh available, not penalized.

2. **Fix streaming SSE total_tokens=0** — root cause of remaining invisible burn for opencode_go (33% NULL) and ollama_cloud_2 (99% NULL).

3. **Fix routstr_probe.py** — 401 (stale key) and 405 (daemon API changed). Without measured rates, Kalman can't converge for T5 providers.

4. **Verify opencode_go key health** — ensure the 401 failures are fully recovered so the router sends deepseek traffic there instead of NeuralWatt.

5. **Phase 0: Capacity tracking table** — add subscription_profitability table, start collecting data for monthly renewal decisions. No pricing change.

6. **Phase 1-2: T1 LQG controller** — replace open-loop time decay with closed-loop controller. Only for z.ai quota providers. After current formula proves stable.

---

## 8. Dual Pricing Surface: Internal Routing vs External Sell Price

### 8.1 The Problem

Felix identified a critical risk (2026-08-24):

> "There is a risk that we run at a loss for an entire month if we expose these prices on routstr. Expose these prices to our live router which always chooses the cheapest endpoint, but lets make sure that the prices that the live router exposes to real users on routstr are high enough to ensure that we always make a profit when selling the tokens to a third party over routstr."

The pricing model in §2 defines **internal routing prices** — artificially low for sunk-cost providers ($0.001/M for T3/T4, $0.001 × time_decay for T1). These are correct for **routing** (attract traffic to sunk-cost resources) but **wrong for billing** — if we charge a third party $0.001/M on routstr, we lose money because the real amortized cost is higher ($20/mo ÷ 200M tokens = $0.023/M for z.ai).

### 8.2 Two Independent Price Surfaces

| | Surface 1: Internal Routing Price | Surface 2: External Sell Price |
|---|---|---|
| **Function** | `compute_effective_price()` | `compute_sell_price()` (new, to be implemented) |
| **Used by** | `flat_router.py select_provider()` — picks cheapest upstream | `routstr` API — sats/token rate charged to third parties |
| **Purpose** | Attract traffic to sunk-cost providers | Always charge above real cost + profit margin |
| **T1 example** | $0.001 × time_decay (as low as $0.000006/M) | $0.023/M × 1.2 = $0.028/M |
| **T3 example** | $0.001/M | $0.20/M × 1.3 = $0.26/M (if 50M/mo usage) |
| **T5 example** | measured_rate (Kalman) | measured_rate × 1.15 |
| **Can be below cost?** | YES — sunk cost optimization | NO — minimum 1.1× actual cost |
| **Exposed to** | Internal only (never on routstr) | External users (routstr only) |

### 8.3 Sell Price Formula per Tier

The sell price uses **actual cost** (subscription economics), NOT the routing price:

| Tier | Actual Cost Formula | Sell Price |
|------|---------------------|------------|
| T1 (z.ai quota) | `subscription_cost / monthly_quota_tokens` (e.g., $20/mo ÷ 866M = $0.023/M) | actual_cost × (1 + margin) |
| T2 (NeuralWatt Phase A) | `subscription_cost / typical_monthly_usage` | actual_cost × (1 + margin) |
| T2 (NeuralWatt Phase B) | `measured_rate` (Kalman — per-token) | actual_cost × (1 + margin) |
| T3 (opencode_go) | `subscription_cost / historic_avg_monthly_usage` (e.g., $10/mo ÷ 50M = $0.20/M) | actual_cost × (1 + margin) |
| T4 (ollama_cloud) | `subscription_cost / historic_avg_monthly_usage` | actual_cost × (1 + margin) |
| T5 (per-token) | `measured_rate` (from Kalman filter) | actual_cost × (1 + margin) |

### 8.4 Profit Margin

- **Default**: 20% (`DEFAULT_PROFIT_MARGIN = 0.20`)
- **Hard minimum**: 10% — `sell_price ≥ actual_cost × 1.1` always enforced
- **Per-provider configurable**: e.g., opencode_go 30% (unknown capacity = risk premium), per-token providers 15% (competitive market), premium models up to 50%
- **Minimum cannot be overridden**: even if margin config says 5%, the 10% floor applies

### 8.5 Safeguards

1. **Unknown cost**: If actual cost can't be determined → `sell_price = max(measured_rate, FALLBACK_RATE) × (1 + margin)` where `FALLBACK_RATE = $0.50/M` (conservative — better to overcharge)
2. **Never-used provider**: `sell_price = conservative_estimate × (1 + margin)`
3. **Monthly profitability check**: At end of billing period, if `sell_revenue < actual_cost` for any provider → ALERT (not block). Consistent losses → increase margin or remove from routstr.
4. **Zero-cost providers** (friend's key): `sell_price = max(FALLBACK_RATE, actual_cost) × (1 + margin)` — don't undercut our own paid providers.

### 8.6 Implementation

**DESIGN ONLY — no code changes yet.**

When implemented:
- New function `compute_sell_price(provider, model, context)` in `flat_router.py` or a separate `sell_pricing.py` module
- Routstr API exposes `compute_sell_price()` as the sats/token rate
- `select_provider()` continues to use `compute_effective_price()` (Surface 1) — unchanged
- Monthly profitability cron job: queries `api_calls` for routstr traffic, computes revenue vs cost, alerts on loss

### 8.7 Full Design

See `capacity-aware-pricing-design.md` §12 (External Sell Pricing) for the complete design including pseudocode, profit margin config, actual cost computation, and the relationship to profitability tracking (§5).

---
