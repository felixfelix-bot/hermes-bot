# Capacity-Aware Kalman Pricing Model — Design Doc

**Author:** Hermes Agent (manager profile)  
**Date:** 2026-08-24  
**Status:** DESIGN ONLY — Felix reviews before implementation  
**Supersedes:** 5-tier static pricing model (commit `7220cd3`, `db88e0f`)

---

## 0. Executive Summary

Felix identified a fundamental flaw in the current pricing model: **the vicious circle**. When base price is derived from actual usage (subscription_cost / actual_usage), underused providers become *more* expensive, not less — driving even less traffic to them in a death spiral.

This design replaces the observer-only PriceKalman with a **capacity-aware controller** that:

1. Sets base price from **capacity** (what we *could* use), not actual usage
2. Uses a Kalman filter + LQR control law to steer consumption toward **full quota exhaustion** just before reset
3. Tracks **actual usage** in a separate profitability vector for monthly renewal decisions
4. Preserves the tier system as the *capacity model* — the Kalman controller runs *within* each tier

The architecture is an **LQG (Linear Quadratic Gaussian) controller**: a Kalman filter estimates the state (usage rate, projected end-of-period usage), and an LQR computes the optimal price multiplier to minimize the gap between projected usage and capacity at reset time.

---

## 1. The Vicious Circle Problem

### 1.1 Current Flaw

The current PriceKalman (in `price_kalman.py`) observes $/M from actual traffic:

```
State: x = [base_rate, velocity]
Observation: z = cost_usd / total_tokens × 1_000_000
```

For subscription providers (opencode_go $10/mo, ollama_cloud included):
- The Kalman measures `cost_usd = $0` (marginal cost is $0) → base_rate → $0
- OR, if using amortized cost: `base_rate = $10 / actual_usage_in_M`
- If we use 5M tokens: base = $2.00/M → expensive → router avoids → less usage → base rises further
- If we use 500M tokens: base = $0.02/M → cheap → router prefers → more usage → base drops further

**This is a positive feedback loop (vicious circle) for underused providers.**

The current 5-tier model patched this for T3/T4 (flat/included) by hardcoding `$0.001/M` floor — bypassing the Kalman entirely. But this is a band-aid: it removes all price responsiveness for those providers.

### 1.2 The Fix: Base Price from Capacity

```
base_price = subscription_cost / capacity
```

Where **capacity** = what we *could* use in one billing period, not what we *actually* used.

| Provider Type | Capacity Definition | Reset Cycle | Example |
|---|---|---|---|
| Quota (z.ai) | Weekly quota from API | 7 days | ours: ~200M tokens/week |
| Balance (NeuralWatt) | initial_balance / cost_per_token | Monthly | $100 / $0.14/M = 714M tokens |
| Flat (opencode_go) | Plan limit or estimated soft cap | Monthly | See §4 |
| Included (ollama_cloud) | Session + weekly quota from API | 5h / 7d | 500M tokens/session |
| Per-token (PPQ, etc.) | N/A — no capacity | N/A | Pay per token, no exhaustion target |

### 1.3 Why This Breaks the Vicious Circle

- **Underused provider**: actual_usage = 5M, capacity = 500M → base = $10/500M = $0.02/M (cheap)
- Price stays cheap regardless of actual usage → traffic flows in → usage increases
- Base price is **stable** — it only changes when capacity changes (e.g., plan upgrade, quota API reports a different weekly limit)
- The **controller** (not the base price) handles the dynamic adjustment

### 1.4 Base Price Formula per Tier

```
T1 (quota):     base = subscription_cost / weekly_quota_tokens
                 ≈ $20/mo / (200M × 4.33 weeks) = $0.023/M
                 (This is the amortized cost — NOT the routing price.
                  The routing price is set by the controller.)

T2 (balance):   base = initial_balance / max_tokens_per_period
                 = $100 / ($100 / cost_per_M) = cost_per_M
                 (The base IS the per-token cost — no amortization needed.)

T3 (flat):      base = subscription_cost / estimated_capacity
                 If capacity unknown: base = MIN_EFFECTIVE_PRICE ($0.001)
                 (Marginal cost = $0, so base approaches $0)

T4 (included):  base = $0.001 (marginal cost = $0, included in subscription)

T5 (per-token): base = listed_rate (from provider API / catalog)
                 (No capacity — price = actual cost, no controller needed.)
```

---

## 2. The Kalman as Controller (Not Just Observer)

### 2.1 Current Architecture: Observer

```
         traffic ──→ $/M measurement ──→ [PriceKalman] ──→ smoothed $/M
                                                         (used for routing)
```

The PriceKalman is a **passive observer**: it smooths noisy $/M measurements from traffic. It has no objective function, no control signal, no feedback loop. It just estimates the state of the world.

### 2.2 Desired Architecture: LQG Controller

```
         tokens/hour ──→ [ConsumptionKalman] ──→ state estimate (usage_rate, projected_end)
                                                    │
                                                    ▼
                              [LQR Control Law] ──→ price_multiplier
                                                    │
                                                    ▼
                              effective_price = base_price × price_multiplier
                                                    │
                                                    ▼
                                          [Router selects cheapest]
                                                    │
                                                    ▼
                                          traffic flows to provider
                                                    │
                                                    ▼
                                          new tokens/hour measurement
                                                    │
                                                    └──→ (loop back to top)
```

This is a **closed-loop feedback controller**. The Kalman filter estimates the system state, the LQR computes the optimal control action (price multiplier), and the system (traffic) responds to the control action.

### 2.3 Why LQG (Not PID, Not RL)

| Approach | Pros | Cons | Verdict |
|---|---|---|---|
| **PID controller** | Simple, well-understood, no model needed | No multi-objective optimization, no state prediction, hard to tune for systems with delay | Fallback — could work for single-provider control |
| **Kalman + LQR (LQG)** | Optimal for linear systems with Gaussian noise, handles multi-variable state, well-established theory, computes optimal control not just reactive | Assumes linear dynamics (price → traffic is not linear), requires system model | **Primary choice** — the Kalman is already there, just add the control law |
| **MPC (Model Predictive Control)** | Handles constraints (price floor, price ceiling), looks ahead multiple steps, optimal for nonlinear systems | Computationally heavier, requires solver at each step | **Phase 2** — if LQR is too simplistic for the elasticity model |
| **Reinforcement Learning** | No model needed, learns from data, handles nonlinear + non-stationary | Sample hungry, unstable, hard to debug, overkill for this problem | **Not recommended** — we don't have enough traffic volume for RL to converge |

**Recommendation**: LQG (Kalman estimator + LQR controller) as the primary architecture. The Kalman filter is already implemented (`ConsumptionKalman` in `consumption_kalman.py`); we need to add the LQR control law on top. If the linear assumption proves insufficient, upgrade to MPC in Phase 2.

### 2.4 State Vector

The controller maintains a **per-provider state vector**:

```
x = [cumulative_usage, usage_rate, usage_acceleration]
```

| State | Symbol | Source | Units |
|---|---|---|---|
| Cumulative usage this period | `u(t)` | Running sum from `api_calls` table | tokens |
| Usage rate (tokens/hour) | `du/dt` | ConsumptionKalman state[0] | tokens/hour |
| Usage acceleration | `d²u/dt²` | ConsumptionKalman state[2] | tokens/hour² |

This is already what `ConsumptionKalman` tracks — we reuse it directly.

### 2.5 Derived Quantities

From the state vector, the controller computes:

```
time_to_reset     = T_reset - t_now                    (hours)
projected_usage   = u(t) + du/dt × T + 0.5 × d²u/dt² × T²
utilization_proj   = projected_usage / capacity
utilization_gap    = 1.0 - utilization_proj             (positive = underutilized)
time_fraction      = (t_now - t_period_start) / (T_reset - t_period_start)
```

### 2.6 The Control Law (LQR)

The LQR minimizes the quadratic cost:

```
J = Σ [ Q × (capacity - projected_usage)² + R × (Δprice_multiplier)² ]
```

Where:
- `Q` = penalty for not exhausting quota (the primary objective)
- `R` = penalty for price volatility (smoothness constraint)
- `Δprice_multiplier` = change in price multiplier from last period

The optimal control law (for the linearized system) is:

```
price_multiplier = K × [utilization_gap, time_fraction]
```

Where `K` is the LQR gain matrix, computed offline from the system dynamics.

### 2.7 Practical Control Law (Heuristic LQR Approximation)

A full LQR requires a linear system model (how does usage_rate respond to price changes?). We don't have that model yet. As a practical first step, we use a **heuristic control law** that approximates the LQR:

```python
def compute_price_multiplier(
    cumulative_usage: float,      # tokens used this period
    usage_rate: float,             # tokens/hour (from ConsumptionKalman)
    capacity: float,               # tokens (quota / plan limit)
    time_to_reset: float,          # hours
    time_fraction_elapsed: float,  # 0.0 to 1.0
    price_floor: float = 0.01,     # minimum multiplier
    price_ceiling: float = 10.0,   # maximum multiplier
) -> float:
    """Capacity-aware price multiplier.

    Objective: exhaust capacity just before reset.

    - Underutilized (projected < capacity) → lower price (attract traffic)
    - On track (projected ≈ capacity) → hold price
    - Overutilized (projected > capacity) → raise price (conserve)
    - Near reset with remaining capacity → aggressively lower (use it or lose it)
    """
    if time_to_reset <= 0 or capacity <= 0:
        return 1.0  # no capacity constraint

    # Projected usage at reset time
    projected_usage = cumulative_usage + usage_rate * time_to_reset
    utilization_projected = projected_usage / capacity

    # The target: be at 100% utilization at reset time
    # utilization_target(time_fraction) = 1.0
    # But we want to REACH 1.0 at reset, not exceed it early

    # Control error: how far are we from the ideal trajectory?
    # Ideal trajectory: linear from 0 to capacity over the period
    ideal_usage = capacity * time_fraction_elapsed
    actual_usage = cumulative_usage
    tracking_error = (ideal_usage - actual_usage) / capacity  # normalized

    # If tracking_error > 0: we're behind schedule → lower price
    # If tracking_error < 0: we're ahead of schedule → raise price

    # Base multiplier from tracking error
    # Using a proportional controller: multiplier = 1 - K_p × tracking_error
    K_p = 5.0  # proportional gain (tunable)
    multiplier = 1.0 - K_p * tracking_error

    # End-of-period urgency boost: if close to reset and still have capacity,
    # aggressively lower price to attract traffic
    if time_fraction_elapsed > 0.7:
        remaining_fraction = 1.0 - time_fraction_elapsed
        urgency_boost = 1.0 - (remaining_fraction * 2.0)  # ramps to 0 as t→reset
        urgency_factor = max(0.01, urgency_boost)
        multiplier *= urgency_factor

    # Clamp to safe range
    return max(price_floor, min(price_ceiling, multiplier))
```

### 2.8 Traffic Elasticity Model

The controller assumes that lowering a provider's price will attract more traffic. But how much more? This is the **elasticity** question.

In our system, the router (`select_provider()`) picks the **cheapest** provider. So the elasticity is **not a smooth demand curve** — it's a **step function**:

- If provider A's price drops below provider B's price, ALL traffic that was going to B now goes to A (for models both serve).
- If provider A's price is already cheapest, lowering it further doesn't attract more traffic (there's no one to steal from).

This means the controller's effect on traffic is **discontinuous**: a small price change can cause a large traffic shift (if it crosses the next-cheapest provider's price), or zero shift (if it doesn't).

**Implication for the control law**: The LQR's linear assumption is violated. The system is piecewise-linear (or more precisely, a switching system). Options:

1. **Accept the approximation**: The linear LQR will oscillate around the switching point but will converge in practice because the ConsumptionKalman smooths the rate measurements. The oscillation manifests as the price multiplier hovering near the competitor's price — which is actually the correct equilibrium.

2. **Use MPC**: Model the switching behavior explicitly. At each control step, check whether lowering the price would cross the next-cheapest provider's price, and predict the resulting traffic surge. This is more accurate but requires a solver.

3. **Use a logarithmic demand model**: Approximate the step function with `traffic ∝ 1/price^α` (where α is the elasticity parameter). This gives a smooth, concave demand curve that's closer to reality than linear but simpler than MPC. The controller becomes:

```
projected_usage = f(current_rate × (current_price / new_price)^α)
```

Where α is estimated from historical data (how did traffic shift when prices changed in the past?).

**Recommendation**: Start with option 1 (linear LQR approximation). The oscillation is acceptable — it's the price discovery mechanism. Upgrade to option 3 (logarithmic model) if oscillation is problematic, using historical traffic data to estimate α.

### 2.9 Cross-Provider Awareness

The controller needs to know about **other providers' prices** to predict traffic response:

- If we lower opencode_go's price, does traffic come from NeuralWatt or from ollama_cloud?
- Answer: traffic comes from whichever provider was previously cheapest among those serving the same model.
- The controller should receive the **current sorted candidate list** (from `select_provider()`) as input, so it knows the competitive landscape.

```python
# In the control loop:
candidates = select_provider(model="glm-5.2")
# candidates[0] is the current cheapest, candidates[1] is the next cheapest
# If we lower our price below candidates[1].effective_cost, we capture all their traffic
# If our price is already below candidates[1], lowering further gains nothing
next_competitor_price = candidates[1].effective_cost if len(candidates) > 1 else float('inf')
```

The controller can use `next_competitor_price` as a **reference signal**: if the current provider is underutilized and its price is above the next competitor, lowering to just below the competitor will capture their traffic. If the provider is already cheapest, further lowering won't help — the controller should instead signal that the provider is "full" (no more traffic available at any price).

---

## 3. The Control Loop

### 3.1 Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    CONTROL LOOP (per provider)                    │
│                                                                    │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────────────┐   │
│  │  Observe    │───→│   Predict    │───→│   Control (LQR)    │   │
│  │             │    │              │    │                    │   │
│  │ tokens in   │    │ projected =  │    │ multiplier = f(    │   │
│  │ last period │    │  current +   │    │  utilization_gap,  │   │
│  │             │    │  rate × T    │    │  time_fraction,     │   │
│  └─────────────┘    └──────────────┘    │  competitor_price) │   │
│                                          └────────┬───────────┘   │
│                                                   │               │
│                                                   ▼               │
│                                          ┌────────────────┐      │
│                                          │  Set Price     │      │
│                                          │                │      │
│                                          │ effective =    │      │
│                                          │  base × mult   │      │
│                                          └────────┬───────┘      │
│                                                   │               │
│                                                   ▼               │
│                                          ┌────────────────┐      │
│                                          │  Router Picks   │      │
│                                          │  Cheapest       │      │
│                                          └────────┬───────┘      │
│                                                   │               │
│                                                   ▼               │
│                                          ┌────────────────┐      │
│                                          │  Traffic Flows │      │
│                                          │  to Provider    │      │
│                                          └────────┬───────┘      │
│                                                   │               │
│                                                   ▼               │
│                                          ┌────────────────┐      │
│                                          │  Measure New   │      │
│                                          │  Usage Rate     │      │
│                                          └────────┬───────┘      │
│                                                   │               │
│                                                   └───────┐       │
│                                                           │       │
│   ┌───────────────────────────────────────────────────────┘       │
│   │                                                               │
│   ▼                                                               │
│  (back to Observe)                                                 │
│                                                                    │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 Step-by-Step

**a) Observe** (every control period, e.g., 5 minutes):
```python
# Query the DB for tokens consumed in the last period
tokens_this_period = query_tokens_since(provider, last_control_time)
# Feed to ConsumptionKalman
consumption_kalman.update(tokens_this_period)
```

**b) Predict**:
```python
# Get state estimate from ConsumptionKalman
usage_rate = consumption_kalman.x[0, 0]          # tokens/hour
usage_accel = consumption_kalman.x[2, 0]        # tokens/hour²
cumulative_usage = consumption_kalman.tokens_used  # this period

# Project to reset time
time_to_reset = reset_timestamp - now
projected_usage = cumulative_usage + usage_rate * time_to_reset + 0.5 * usage_accel * time_to_reset**2
```

**c) Compare**:
```python
utilization_projected = projected_usage / capacity
utilization_gap = 1.0 - utilization_projected  # >0: underutilized, <0: overutilized
time_fraction_elapsed = (now - period_start) / (reset_timestamp - period_start)
```

**d) Adjust Price**:
```python
# Compute control signal (price multiplier)
multiplier = compute_price_multiplier(
    cumulative_usage, usage_rate, capacity,
    time_to_reset, time_fraction_elapsed,
)
# Set effective price
effective_price = base_price * multiplier
# Apply floor
effective_price = max(MIN_EFFECTIVE_PRICE, effective_price)
```

**e) Traffic Responds**:
```python
# The router (select_provider) compares effective_price across providers
# and picks the cheapest. Lower price → more traffic → higher usage_rate.
# This is the system's response to the control signal.
```

**f) Measure New Rate → Repeat**:
```python
# Next control period: measure tokens consumed, feed to ConsumptionKalman,
# which updates the state estimate. The new state reflects the traffic
# response to the previous price change. Loop continues.
```

### 3.3 Control Period

The control loop runs on a fixed period. Options:

| Period | Pros | Cons | Recommendation |
|---|---|---|---|
| Per request | Most responsive | Noisy (single requests are bursty), high overhead | No |
| 1 minute | Responsive | Still noisy for low-traffic providers | Maybe |
| 5 minutes | Good balance | 288 control steps/day | **Yes** (initial) |
| 1 hour | Smooth | Slow to react, quota may be wasted before adjustment | Maybe for Phase 2 |

**Recommendation**: 5-minute control period, matching the existing `_OLLAMA_QUOTA_CACHE_TTL` and `_CREDIT_SPEND_CACHE_TTL`. The ConsumptionKalman is updated per-request (fine-grained), but the price multiplier is recomputed every 5 minutes (coarse-grained). This separates the measurement timescale from the control timescale.

### 3.4 Convergence Behavior

Scenario: z.ai "ours" key, weekly quota = 200M tokens, reset in 7 days.

```
Day 0:  usage_rate = 0 (no traffic yet)
        projected = 0 → utilization_gap = 1.0 (massively underutilized)
        multiplier → 0.01 (minimum) → effective price extremely low
        → router sends ALL traffic here → usage_rate jumps

Day 1:  usage_rate = 30M/day (from yesterday's traffic)
        projected = 30M × 7 = 210M > 200M capacity
        utilization_gap = -0.05 (slightly overutilized)
        multiplier → 1.2 → price rises slightly → some traffic diverts to friend
        → usage_rate drops to ~28M/day

Day 3:  cumulative = 84M, rate = 28M/day, time_to_reset = 4 days
        projected = 84 + 28×4 = 196M → utilization_gap = 0.02 (close)
        multiplier → 0.95 → price slightly below competitor → stable

Day 6:  cumulative = 168M, rate = 28M/day, time_to_reset = 1 day
        projected = 168 + 28 = 196M → still under by 4M
        time_fraction = 0.86 → urgency boost kicks in
        multiplier → 0.3 → price drops → traffic surges

Day 7:  cumulative = 198M, rate = 40M/day, time_to_reset = 2 hours
        projected = 198 + 40×0.083 = 201M → slightly over
        multiplier → 1.5 → price rises → traffic diverts
        → final usage at reset: ~200M (target hit!)
```

### 3.5 Stability Analysis

The control loop is stable if:
1. **The proportional gain K_p is not too high**: If K_p > 1/tracking_error_max, the multiplier can swing from 0 to ∞ in one step. We clamp to [0.01, 10.0].
2. **The measurement delay is less than the control period**: We measure usage every 5 minutes and adjust every 5 minutes — no delay.
3. **The system response (traffic → usage_rate) is monotonic**: Lower price → more traffic (always true in our router, which picks cheapest).
4. **The ConsumptionKalman converges**: It does — it's a standard Kalman filter with constant-acceleration model, well-conditioned for token-rate estimation.

The main instability risk is **oscillation at the switching point** — when our price is very close to a competitor's price, small adjustments cause traffic to flip-flop. The ConsumptionKalman smooths this (it integrates over 5 minutes), and the price volatility penalty in the LQR cost function dampens rapid changes.

---

## 4. The Problem of Unknown Capacity

### 4.1 Provider Capacity Classification

| Provider | Capacity Known? | Source | Reset Cycle | Controller Needed? |
|---|---|---|---|---|
| z.ai (ours, friend) | **YES** | quota_cache (API: 5h/weekly/monthly windows) | 5h / 7d / 30d | **YES** — hard quota, use-it-or-lose-it |
| ollama_cloud | **YES** | ollama.com/api/usage (session + weekly) | 5h / 7d | **YES** — hard quota |
| NeuralWatt | **YES** | balance / per-token cost | Monthly | **YES** — balance depletes |
| opencode_go | **UNKNOWN** | $10/mo flat, no stated limit | Monthly | **MAYBE** — see §4.2 |
| PPQ | **N/A** | Pay per token | N/A | **NO** — no capacity to exhaust |
| DeepInfra | **N/A** | Pay per token (credit balance) | N/A | **NO** (balance tracking only) |
| OpenRouter | **N/A** | Pay per token (credit balance) | N/A | **NO** |
| Telnyx | **N/A** | Pay per token (credit balance) | N/A | **NO** |
| Routstr/Routstrd | **N/A** | Cashu balance | N/A | **NO** (balance tracking only) |

### 4.2 Unknown Capacity: opencode_go

opencode_go is a $10/mo flat-rate plan. The provider doesn't publish a usage limit. Options:

**Option A: Treat as unlimited (no controller)**
- Marginal cost = $0 → price = MIN_EFFECTIVE_PRICE ($0.001/M)
- No controller needed — always want maximum usage
- Risk: if there's a hidden soft cap (rate limiting, throttling after N requests), we'll hit it unprepared
- This is what the current T3 tier does

**Option B: Estimate capacity from history**
```python
# Estimated capacity = max(daily_usage_last_30_days) × 30
# Or: 95th percentile of daily usage × 30
# This is a "we've never used more than X, so X is probably near the limit" heuristic
estimated_capacity = max(daily_usage_history) * 30
```
- Conservative — assumes past max is the ceiling
- If the provider has no limit, this underestimates capacity (which is fine — it means the controller will always try to attract more traffic)
- If the provider DOES have a limit, we'll discover it when we hit it (429/422 errors)

**Option C: Probe-based capacity discovery**
- Send increasing traffic and watch for rate limiting
- When we get a 429, we've found the capacity
- Then set capacity = usage_at_429 × 0.9 (safety margin)
- Requires active probing — not practical for production

**Recommendation**: Option A for now (treat as unlimited, no controller). Add a **soft cap detection** mechanism: if we start seeing 429s or throttling from opencode_go, automatically switch to Option B (estimate capacity from pre-throttling usage). This is the same pattern as the existing circuit breaker / backoff system in `key_health`.

### 4.3 Unknown Capacity: ollama_cloud

ollama_cloud has a known capacity from the usage API (session.usage + weekly usage). The `_get_ollama_quota_status()` function already extracts this. The controller can use this directly:

```python
capacity_session = DEFAULT_SESSION_LIMIT  # 500M tokens per 5h
capacity_weekly = ...  # from API
# Controller targets the most restrictive window
```

### 4.4 Flat-Rate / Included Providers: Is the Controller Necessary?

For providers where marginal cost = $0 (opencode_go, ollama_cloud included tier):

- **If truly unlimited**: No controller needed. Price = MIN_EFFECTIVE_PRICE. Always attract maximum traffic. The controller's objective (exhaust capacity) is meaningless when capacity = ∞.
- **If limited by quota**: Controller IS needed. ollama_cloud has a 5h session limit — the controller should ensure we use the full session quota before it resets.
- **If limited by rate/throttle**: Controller is helpful but secondary. The main signal is 429 responses, not price adjustments.

**Decision**:
- ollama_cloud → controller ON (has known quota)
- opencode_go → controller OFF (treat as unlimited, price = $0.001)
- If opencode_go starts throttling → enable controller with estimated capacity

### 4.5 Per-Token Providers: No Controller

Per-token providers (PPQ, DeepInfra, OpenRouter, Telnyx, Routstr) have no capacity to exhaust. Their price should be the **actual per-token cost** (measured by the existing PriceKalman observer). The controller is not applicable.

However, for **credit-based providers** (DeepInfra, Telnyx, PPQ balance), there IS a depleting balance. The controller could target "use up the credit balance before it runs out" — but this is a different objective (maximize value, not exhaust quota). The current depletion penalty (T2) handles this adequately.

---

## 5. The Profitability Vector

### 5.1 New DB Table: `subscription_profitability`

```sql
CREATE TABLE IF NOT EXISTS subscription_profitability (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    provider            TEXT NOT NULL,
    period_start        TEXT NOT NULL,       -- ISO date (e.g., '2026-08-01')
    period_end          TEXT NOT NULL,         -- ISO date (e.g., '2026-08-31')
    subscription_cost   REAL NOT NULL,        -- USD paid for the period
    actual_usage_tokens INTEGER NOT NULL DEFAULT 0,  -- total tokens served
    actual_cost_usd     REAL NOT NULL DEFAULT 0,     -- estimated cost (for per-token: actual; for subscription: $0)
    tasks_completed     INTEGER NOT NULL DEFAULT 0,  -- API calls that returned 200
    tasks_failed        INTEGER NOT NULL DEFAULT 0,  -- API calls that returned error
    avg_latency_ms       REAL,                -- average response latency
    effective_rate      REAL,                 -- $/M = subscription_cost / (actual_usage_tokens / 1M)
    value_score         REAL,                 -- composite quality metric (see §5.3)
    profitability_ratio REAL,                 -- value_score / subscription_cost
    decision            TEXT,                -- 'RENEW' | 'CANCEL' | 'DOWNGRADE' | 'UPGRADE' | 'PENDING'
    decision_reason     TEXT,                -- human-readable explanation
    updated_ts          REAL NOT NULL
);
```

### 5.2 Data Sources

| Column | Source | Query |
|---|---|---|
| `actual_usage_tokens` | `api_calls` table | `SELECT SUM(total_tokens) FROM api_calls WHERE key_name = ? AND ts BETWEEN ? AND ?` |
| `actual_cost_usd` | `api_calls` table | `SELECT SUM(cost_usd) FROM api_calls WHERE key_name = ? AND ts BETWEEN ? AND ?` |
| `tasks_completed` | `api_calls` table | `SELECT COUNT(*) FROM api_calls WHERE key_name = ? AND status_code = 200 AND ts BETWEEN ? AND ?` |
| `tasks_failed` | `api_calls` table | `SELECT COUNT(*) FROM api_calls WHERE key_name = ? AND status_code != 200 AND ts BETWEEN ? AND ?` |
| `avg_latency_ms` | `api_calls` table | `SELECT AVG(duration_ms) FROM api_calls WHERE key_name = ? AND ts BETWEEN ? AND ?` |
| `subscription_cost` | Config (hardcoded per provider) | `SUBSCRIPTION_COSTS = {"opencode_go": 10.0, "neuralwatt": 100.0, "ours": 20.0, ...}` |
| `effective_rate` | Computed | `subscription_cost / (actual_usage_tokens / 1_000_000)` |
| `value_score` | Computed | See §5.3 |
| `profitability_ratio` | Computed | `value_score / subscription_cost` |
| `decision` | Computed (monthly job) | See §5.4 |

### 5.3 Value Score

The "estimated value" Felix asks about is: **what did we get for our money?**

Options:
1. **Tokens served** — simple but doesn't capture quality
2. **Tasks completed** — better, captures successful responses
3. **Quality-weighted tasks** — best, accounts for response quality

**Proposed composite metric:**

```python
def compute_value_score(
    tasks_completed: int,
    avg_latency_ms: float,
    failure_rate: float,  # tasks_failed / (tasks_completed + tasks_failed)
    model_quality_factor: float = 1.0,  # per-model quality weight (e.g., glm-5.2 = 1.0, glm-4.5-flash = 0.7)
) -> float:
    """Composite value score for a provider in a billing period.

    Higher = more valuable. Combines:
    - Volume: tasks completed (linear)
    - Speed: latency bonus (faster = better, up to a cap)
    - Reliability: failure penalty
    - Quality: model-level quality factor
    """
    if tasks_completed <= 0:
        return 0.0

    # Latency score: 1.0 at 500ms, 0.5 at 5000ms, 0.0 at 30000ms
    latency_score = max(0.0, min(1.0, 1.0 - (avg_latency_ms - 500) / 29500))

    # Reliability: 1.0 at 0% failure, drops linearly
    reliability = max(0.0, 1.0 - failure_rate)

    # Composite
    return tasks_completed * latency_score * reliability * model_quality_factor
```

### 5.4 Decision Logic

Monthly job (cron, 1st of each month) evaluates each subscription:

```python
def evaluate_subscription(provider: str, period: tuple[str, str]) -> tuple[str, str]:
    """Decide whether to renew, cancel, or adjust a subscription.

    Returns (decision, reason).
    """
    row = query_profitability(provider, period)
    if row is None:
        return ("PENDING", "no data yet")

    cost = row['subscription_cost']
    usage_M = row['actual_usage_tokens'] / 1_000_000
    tasks = row['tasks_completed']
    effective_rate = cost / max(usage_M, 0.001)
    profitability = row['profitability_ratio']

    # Decision thresholds (tunable, per-provider)
    if tasks == 0:
        return ("CANCEL", f"0 tasks completed in {period}. Pure waste: ${cost:.2f}/mo")

    if effective_rate > 5.0:  # > $5/M effective — very expensive
        return ("CANCEL", f"Effective ${effective_rate:.2f}/M — more expensive than per-token alternatives")

    if effective_rate > 1.0 and tasks < 50:
        return ("DOWNGRADE", f"Low utilization: {tasks} tasks at ${effective_rate:.2f}/M. Consider cheaper plan.")

    if effective_rate < 0.05 and tasks > 100:
        return ("UPGRADE", f"High utilization: {tasks} tasks at ${effective_rate:.4f}/M. Consider higher tier for more capacity.")

    return ("RENEW", f"{tasks} tasks, {usage_M:.1f}M tokens, ${effective_rate:.4f}/M effective. Good value.")
```

### 5.5 Monthly Report Example

```
=== SUBSCRIPTION PROFITABILITY REPORT — August 2026 ===

opencode_go:    $10.00/mo | 487M tokens | 234 tasks | $0.0205/M effective | 99.1% success | 1.2s avg latency
  → RENEW (excellent value, $0.02/M is cheaper than any per-token provider)

ollama_cloud:   $20.00/mo | 1.2B tokens | 856 tasks | $0.0167/M effective | 97.8% success | 0.8s avg latency
  → RENEW (best value provider, heavy usage)

z.ai (ours):    $20.00/mo | 340M tokens | 412 tasks | $0.0588/M effective | 99.5% success | 0.5s avg latency
  → RENEW (reliable, fast — premium quality justified)

neuralwatt:     $100.00/mo | 12M tokens | 8 tasks | $8.33/M effective | 87.5% success | 3.5s avg latency
  → CANCEL (8 tasks for $100? Any per-token provider is cheaper. $8.33/M vs PPQ $0.80/M)
```

### 5.6 Profitability vs Pricing: Separation of Concerns

**Critical principle**: The profitability vector is a **REPORTING** metric, not a **PRICING** input.

- **Pricing** uses: base_price (from capacity) × controller_multiplier (from projected usage)
- **Profitability** uses: actual_usage × subscription_cost → decision for next month

The controller NEVER sees actual_usage as a pricing input. This is what breaks the vicious circle:

```
OLD (vicious circle):
  price ← actual_usage (feedback loop: less usage → higher price → less usage)

NEW (broken loop):
  price ← capacity (fixed) × controller_multiplier (from projected usage vs capacity)
  profitability ← actual_usage (one-way: reporting only, no feedback to price)
```

---

## 6. How This Replaces or Augments the Current Tier System

### 6.1 The Tier System's Role

The tier system (`PROVIDER_TIER` in `flat_router.py`) classifies providers by cost structure:

| Tier | Providers | Cost Model | Current Pricing |
|---|---|---|---|
| T1 (quota) | z.ai keys | Sunk cost (subscription), hard quota | $0.001 × time_decay × peak × health |
| T2 (balance) | NeuralWatt | Prepaid kWh (monthly), then pay-per-token | **Phase A**: $0.001 × time_decay (monthly) \|\| **Phase B**: measured rate |
| T3 (flat) | opencode_go | $10/mo, no stated limit | $0.001 (floor) |
| T4 (included) | ollama_cloud | Included in subscription | $0.001 (floor) |
| T5 (per-token) | PPQ, DeepInfra, etc. | Pay per token | Kalman-measured rate |

### 6.2 The Kalman Controller's Role

The Kalman controller adjusts the **effective price** within each tier to achieve the capacity exhaustion objective.

### 6.3 Proposed Architecture: Tiers Define Capacity, Controller Sets Price

```
┌─────────────────────────────────────────────────────────────────┐
│                    EFFECTIVE PRICE PIPELINE                     │
│                                                                 │
│  ┌──────────┐    ┌───────────────┐    ┌────────────────────┐   │
│  │ Tier     │───→│ Base Price    │───→│ Controller         │   │
│  │ defines: │    │ = sub_cost /  │    │ adjusts multiplier │   │
│  │          │    │   capacity    │    │                    │   │
│  │ - cost   │    │              │    │ effective = base ×  │   │
│  │ - cycle  │    │ (stable,     │    │  multiplier        │   │
│  │ - capacity│   │  no vicious  │    │                    │   │
│  │ - reset  │    │  circle)     │    │ (dynamic,          │   │
│  │          │    │              │    │  closed-loop)       │   │
│  └──────────┘    └───────────────┘    └────────┬───────────┘   │
│                                                 │               │
│                                                 ▼               │
│                                        ┌────────────────┐      │
│                                        │  Floor & Clamp  │      │
│                                        │  max($0.001,    │      │
│                                        │   effective)    │      │
│                                        └────────┬───────┘      │
│                                                 │               │
│                                                 ▼               │
│                                        ┌────────────────┐      │
│                                        │  Health Gate    │      │
│                                        │  (unchanged)    │      │
│                                        └────────┬───────┘      │
│                                                 │               │
│                                                 ▼               │
│                                        ┌────────────────┐      │
│                                        │  Router Picks   │      │
│                                        │  Cheapest       │      │
│                                        └────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

### 6.4 Tier-by-Tier Integration

**T1 (quota — z.ai keys)**:
- Capacity: from quota_cache (weekly window: used_pct, resets_at)
- Base price: subscription_cost / weekly_quota_tokens (stable)
- Controller: adjusts multiplier to exhaust weekly quota by reset time
- **Replaces**: the current `time_decay = days_to_reset / 7` with a closed-loop controller
- The controller IS the time_decay, but adaptive: it considers actual usage rate, not just time elapsed

**T2 (balance — NeuralWatt)**:
- TWO-PHASE STATE MACHINE (see §11.2 for full design):
  - Phase A (included kWh available): effective = $0.001 × time_decay (monthly, if kWh doesn't carry over) or $0.001 (if carries over)
  - Phase B (kWh exhausted): effective = measured_rate (Kalman, like T5)
- Transition: remaining_kWh ≤ 0 (from balance bridge API)
- **Replaces**: the current `depletion_penalty = (1 - remaining/initial) × 2.0` — this was WRONG: price should NOT rise as balance drops. The included kWh is a sunk cost.
- Correction factor (0.2762): measurement only (applied in `_extract_cost`, NOT to routing price)
- kWh carry-over: configuration question (default: no carry-over → monthly time decay, like T1 but 30-day cycle)

**T3 (flat — opencode_go)**:
- Capacity: ∞ (treated as unlimited, see §4.2)
- Base price: $0.001 (marginal cost = $0)
- Controller: OFF (no capacity to exhaust)
- **Unchanged**: current $0.001 floor stays

**T4 (included — ollama_cloud)**:
- Capacity: from ollama.com/api/usage (session + weekly)
- Base price: $0.001 (marginal cost = $0)
- Controller: ON — adjust price to exhaust session quota before reset
- **Replaces**: the current $0.001 floor with a dynamic price that considers quota utilization
- When quota is fresh (5h window just started): price = $0.001 (attract traffic)
- When quota is running low and time is running out: controller lowers price further (use it or lose it)
- When quota is exhausted: health gate returns inf (provider unavailable)

**T5 (per-token — PPQ, DeepInfra, etc.)**:
- Capacity: N/A
- Base price: listed_rate (from provider API / catalog)
- Controller: OFF (no capacity to exhaust)
- **Unchanged**: PriceKalman observer measures actual $/M, smooths it. No controller.

### 6.5 Does the Tier System Become Unnecessary?

**No.** The tier system remains the **configuration layer** — it defines what capacity, cost, and reset cycle each provider has. The Kalman controller is the **execution layer** — it uses the tier configuration to compute the price.

```
Tier system:    "z.ai is T1, weekly quota, resets Monday"
                 → defines capacity = 200M, reset = Monday 00:00 UTC

Controller:      "it's Sunday 22:00, usage_rate = 30M/day, projected = 196M/200M"
                 → multiplier = 0.3 → effective price drops → traffic surges
```

Without the tier system, the controller doesn't know what capacity to target or when the reset is. The tiers are the **problem definition**; the controller is the **solution**.

---

## 7. The Transition Plan

### 7.1 Current State

- 5-tier static pricing model live (commit `7220cd3`)
- `compute_effective_price()` in `flat_router.py` applies tier-specific formulas
- PriceKalman (observer) in `price_kalman.py` — 2-state filter, smooths $/M
- ConsumptionKalman in `consumption_kalman.py` — 3-state filter, tracks token burn rate
- LiveRouter in `live_router.py` — maintains per-provider Kalman instances, `select_failover()`
- `flat_router.py` runs in **shadow mode** (does not drive routing yet)
- `best_key()` still drives all production routing

### 7.2 Phased Rollout

#### Phase 0: Capacity Tracking (no pricing change)
**Goal**: Add capacity awareness without changing any prices.
**Changes**:
- Add `provider_capacity` table: `(provider, period_start, period_end, capacity_tokens, reset_timestamp, subscription_cost)`
- Populate from quota_cache (z.ai), ollama usage API, NeuralWatt balance
- Add `subscription_profitability` table (§5.1)
- Monthly cron job to populate profitability report
- **No pricing change** — the existing tier formulas stay
**Risk**: None (read-only additions)
**Rollback**: Drop new tables
**Duration**: 1-2 days

#### Phase 1: Controller for T1 (quota providers) — shadow mode
**Goal**: Run the Kalman controller for z.ai keys in SHADOW mode (log what it WOULD set, don't use it).
**Changes**:
- Implement `CapacityController` class (§7.3)
- Wire into `compute_effective_price()` for T1 providers: compute controller_multiplier AND the existing time_decay, log both
- New column in `flat_router_shadow_decisions`: `controller_multiplier`, `time_decay_multiplier`
- Compare: does the controller converge to the same prices as time_decay? Or does it discover different prices?
**Risk**: None (shadow only)
**Rollback**: Stop logging (no behavior change)
**Duration**: 3-5 days for convergence analysis
**Gate**: Felix reviews shadow data, confirms the controller is stable and the prices are reasonable

#### Phase 2: Controller for T1 — live (replace time_decay)
**Goal**: Replace the static time_decay formula with the Kalman controller for T1 providers.
**Changes**:
- `compute_effective_price()` for T1: use `controller_multiplier` instead of `time_decay`
- Kill switch: flag file `~/.hermes/bot/.disable_capacity_controller` → reverts to time_decay
- Monitor: quota exhaustion rate, end-of-period utilization, price stability
**Risk**: Medium — prices change for z.ai keys, could affect routing behavior
**Rollback**: `touch ~/.hermes/bot/.disable_capacity_controller` (instant revert to time_decay)
**Duration**: 1 week of monitoring
**Gate**: Confirm weekly quota is being exhausted (≥95% utilization by reset), no price instability

#### Phase 3: Controller for T2 (balance) and T4 (included)
**Goal**: Extend controller to NeuralWatt (T2) and ollama_cloud (T4).
**Changes**:
- T2: Controller targets balance exhaustion by period end (replaces depletion_penalty)
- T4: Controller targets session/weekly quota exhaustion (replaces static $0.001 floor for ollama_cloud)
- Kill switch per tier: `.disable_t2_controller`, `.disable_t4_controller`
**Risk**: Medium — T2 price changes could route traffic away from NeuralWatt unexpectedly
**Rollback**: Per-tier kill switches (instant revert to static formulas)
**Duration**: 1 week per tier
**Gate**: Felix reviews each tier separately

#### Phase 4: Full integration
**Goal**: Remove the old static formulas (time_decay, depletion_penalty) for controlled tiers. Keep them as fallback.
**Changes**:
- Old formulas become the kill-switch fallback (already implemented in Phase 2/3)
- The controller is the primary path
- Old formulas are dead code (removed in a future cleanup)
**Risk**: Low (kill switches already tested)
**Duration**: Ongoing monitoring

### 7.3 New Class: `CapacityController`

```python
# capacity_controller.py — NEW FILE

class CapacityController:
    """Capacity-aware pricing controller for subscription/quota providers.

    Implements an LQG-style controller:
    - Kalman estimator (ConsumptionKalman): estimates usage_rate from traffic
    - LQR control law: computes price multiplier to exhaust capacity by reset

    One instance per provider. Maintains its own ConsumptionKalman.

    State (from ConsumptionKalman):
        x = [burn_rate, velocity, acceleration]

    Derived:
        projected_usage = cumulative + rate × time_to_reset
        utilization_gap = 1.0 - projected_usage / capacity

    Control signal:
        price_multiplier = f(utilization_gap, time_fraction, competitor_price)

    Objective:
        minimize (capacity - projected_usage)² + λ × (Δmultiplier)²
    """

    def __init__(
        self,
        provider: str,
        capacity: float,           # tokens per period
        period_start: float,        # epoch timestamp
        reset_timestamp: float,    # epoch timestamp
        base_price: float,         # $/M (subscription_cost / capacity)
        consumption_kalman: ConsumptionKalman | None = None,
        K_p: float = 5.0,          # proportional gain
        K_u: float = 3.0,          # urgency gain (end-of-period boost)
        price_floor_mult: float = 0.01,
        price_ceiling_mult: float = 10.0,
    ):
        self.provider = provider
        self.capacity = capacity
        self.period_start = period_start
        self.reset_timestamp = reset_timestamp
        self.base_price = base_price
        self.ck = consumption_kalman or ConsumptionKalman(
            process_noise=1.0, measurement_noise=1e6)
        self.K_p = K_p
        self.K_u = K_u
        self.price_floor_mult = price_floor_mult
        self.price_ceiling_mult = price_ceiling_mult
        self._last_multiplier = 1.0
        self._cumulative_usage = 0.0

    def observe(self, tokens: float) -> None:
        """Feed a token-consumption observation to the Kalman."""
        self.ck.update(tokens)
        self._cumulative_usage += tokens

    def compute_multiplier(self, now: float | None = None) -> float:
        """Compute the price multiplier for the current state."""
        now = now or time.time()
        time_to_reset = self.reset_timestamp - now
        if time_to_reset <= 0 or self.capacity <= 0:
            return 1.0

        time_fraction = (now - self.period_start) / (
            self.reset_timestamp - self.period_start)
        time_fraction = max(0.0, min(1.0, time_fraction))

        usage_rate = float(self.ck.x[0, 0])  # tokens/hour
        projected_usage = (self._cumulative_usage
                          + usage_rate * time_to_reset)
        utilization_projected = projected_usage / self.capacity

        # Tracking error: positive = behind schedule (underutilized)
        ideal_usage = self.capacity * time_fraction
        tracking_error = (ideal_usage - self._cumulative_usage) / self.capacity

        # Proportional control
        multiplier = 1.0 - self.K_p * tracking_error

        # End-of-period urgency (use it or lose it)
        if time_fraction > 0.7:
            remaining = 1.0 - time_fraction
            urgency = max(0.01, 1.0 - remaining * 2.0)
            multiplier *= (1.0 - self.K_u * (1.0 - urgency))

        # Overutilization: raise price to conserve
        if utilization_projected > 1.0:
            overshoot = utilization_projected - 1.0
            multiplier *= (1.0 + overshoot * self.K_p)

        # Smooth: dampen rapid changes
        smoothed = 0.7 * multiplier + 0.3 * self._last_multiplier
        self._last_multiplier = smoothed

        return max(self.price_floor_mult,
                   min(self.price_ceiling_mult, smoothed))

    @property
    def effective_price(self) -> float:
        """Current effective $/M price (base × multiplier)."""
        return max(MIN_EFFECTIVE_PRICE,
                   self.base_price * self.compute_multiplier())

    @property
    def projected_utilization(self) -> float:
        """Projected utilization at reset time (0.0 = unused, 1.0 = exactly full)."""
        now = time.time()
        time_to_reset = self.reset_timestamp - now
        if time_to_reset <= 0:
            return 1.0
        rate = float(self.ck.x[0, 0])
        projected = self._cumulative_usage + rate * time_to_reset
        return projected / self.capacity if self.capacity > 0 else 0.0
```

### 7.4 Integration into `compute_effective_price()`

```python
# In flat_router.py, compute_effective_price() for T1 (quota):

def compute_effective_price(provider, base_rate, context=None):
    # ... existing code ...

    if tier == "quota":
        # NEW: use CapacityController if available
        controller = _get_capacity_controller(provider)
        if controller is not None and not _is_controller_disabled():
            multiplier = controller.compute_multiplier()
            effective = base_rate * multiplier  # wait, base_rate for T1 is MIN_EFFECTIVE_PRICE
            # Actually: effective = base_price_from_capacity × multiplier
            # where base_price_from_capacity = subscription_cost / weekly_quota
            effective = controller.base_price * multiplier
            return max(MIN_EFFECTIVE_PRICE, effective)
        else:
            # FALLBACK: existing time_decay formula (unchanged)
            time_decay = ctx.get("time_decay", _compute_time_decay(provider))
            decay_floor = max(0.0001, time_decay)
            effective = MIN_EFFECTIVE_PRICE * decay_floor * peak_factor * health_factor
            return effective
```

### 7.5 Kill Switch

```python
# Kill switch: flag file disables the controller, reverts to static formulas
_CAPACITY_CONTROLLER_FLAG = os.path.expanduser("~/.hermes/bot/.disable_capacity_controller")

def _is_controller_disabled() -> bool:
    return os.path.exists(_CAPACITY_CONTROLLER_FLAG)
```

**Rollback procedure**:
```bash
# Instant rollback — reverts ALL tiers to static formulas
touch ~/.hermes/bot/.disable_capacity_controller

# Per-tier rollback (Phase 3+)
touch ~/.hermes/bot/.disable_t1_controller  # z.ai keys → time_decay
touch ~/.hermes/bot/.disable_t2_controller  # NeuralWatt → depletion_penalty
touch ~/.hermes/bot/.disable_t4_controller  # ollama_cloud → $0.001 floor
```

No restart needed — the flag file is checked on every `compute_effective_price()` call.

---

## 8. Critical Design Decisions

### 8.1 Is the Kalman Filter the Right Tool for Control?

**Strictly speaking, no.** A Kalman filter is an *estimator*, not a *controller*. It estimates the state of a system from noisy observations. To *control* the system, you need a control law (LQR, PID, MPC) that uses the Kalman's state estimate to compute a control action.

**However**, the LQG (Linear Quadratic Gaussian) framework combines a Kalman estimator with an LQR controller into a single optimal control system. This is well-established in control theory and is exactly what we need:

- **Kalman estimator** (already exists: `ConsumptionKalman`): estimates usage_rate from noisy token observations
- **LQR controller** (new: `CapacityController`): computes price multiplier to minimize the cost function

The Kalman is the right tool for the *estimation* half. The LQR is the right tool for the *control* half. Together they form LQG.

### 8.2 Is the Simple Time Decay Sufficient?

The current T1 time_decay (`days_to_reset / 7`) is an **open-loop controller**: it assumes usage rate is constant and that lowering price linearly over time will exhaust the quota. It doesn't account for:

- **Variable usage rate**: if traffic is bursty (more during peak hours, less at night), the linear decay may overshoot or undershoot
- **Competitor pricing**: if a competitor drops their price, our time_decay doesn't react
- **Early overconsumption**: if we burn 80% of quota in day 1, time_decay still shows `6/7 = 0.857` (high price), but we should be raising the price to conserve

The Kalman controller is **closed-loop**: it observes actual usage rate and adjusts. It handles all three cases above.

**Verdict**: Time decay is sufficient for the *average* case. The controller adds value for the *edge* cases (bursty traffic, competitor price changes, early overconsumption). Given the complexity cost, the controller is worth implementing for T1 (hard quota, use-it-or-lose-it) but probably overkill for T3/T4 (unlimited, no exhaustion target).

### 8.3 Do We Need Cross-Provider Awareness in the Controller?

**Yes, but softly.** The controller doesn't need to model the full competitive landscape. It needs one piece of information: **the next cheapest competitor's price**.

If our effective price is above the next competitor, we get zero traffic regardless of our multiplier. If our effective price is below the next competitor, we get all the traffic for shared models.

The controller can use this as a **saturation signal**: if we lower the multiplier but usage_rate doesn't increase (because we're already the cheapest), the controller has reached the "traffic floor" — there's no more traffic to capture. In this case, the controller should stop lowering the price (the penalty for price volatility in the LQR cost function handles this naturally).

```python
# In the control loop, after setting the new multiplier:
if usage_rate_unchanged_after_price_decrease:
    # We're already cheapest — no more traffic to capture
    # Stop lowering the price (the LQR's volatility penalty does this)
    pass
```

This doesn't require explicit cross-provider modeling — the ConsumptionKalman's measurement naturally reflects whether traffic responded to the price change.

### 8.4 Are We Overcomplicating This?

**Partially yes, partially no.**

The **profitability vector** (§5) is simple and high-value — it's just a monthly report. No complexity concern.

The **capacity-based base price** (§1) is simple and breaks the vicious circle. No complexity concern.

The **Kalman controller** (§2-3) is the complex part. The question is: does it provide enough value over the existing time_decay to justify the implementation cost?

**Arguments for simplification**:
- Time decay already works reasonably for T1 (the main quota case)
- The controller adds a state estimation component (already exists) and a control law (new but small)
- The main risk is oscillation / instability, which requires tuning

**Arguments against simplification**:
- Felix explicitly asked for the Kalman to "set the base price depending on how much we could be using it" — this IS the controller
- The vicious circle problem is real and the current model doesn't solve it for T3/T4
- The controller provides closed-loop feedback that no static formula can match

**Recommendation**: Implement the controller for T1 (quota) only in Phase 1-2. Evaluate its performance vs time_decay. If the improvement is marginal, keep time_decay as the primary and use the controller as a refinement. If the improvement is significant, roll out to T2/T4. The phased plan (§7) already supports this — each tier has its own kill switch and can be independently evaluated.

---

## 9. File Changes Summary

### New Files

| File | Purpose |
|---|---|
| `~/merchant-routing-engine/src/capacity_controller.py` | `CapacityController` class (§7.3) |
| `~/merchant-routing-engine/src/profitability_tracker.py` | Monthly profitability report generator |

### Modified Files

| File | Changes |
|---|---|
| `~/.hermes/bot/flat_router.py` | `compute_effective_price()` — use controller for T1 (with kill switch fallback to time_decay) |
| `~/.hermes/bot/zai_proxy.py` | Wire controller observation into `_record_spend()` (feed tokens to controller) |

### New DB Tables

| Table | Purpose |
|---|---|
| `provider_capacity` | Per-provider capacity configuration (capacity, reset time, subscription cost) |
| `subscription_profitability` | Monthly profitability report (§5.1) |
| `controller_state` | Controller state snapshots (for observability and tuning) |

### New DB Columns

| Table | Column | Purpose |
|---|---|---|
| `flat_router_shadow_decisions` | `controller_multiplier` | What the controller WOULD set (shadow mode) |
| `flat_router_shadow_decisions` | `time_decay_multiplier` | What time_decay WOULD set (for comparison) |

---

## 10. Appendix: Key Formulas Reference

### Base Price (per tier)
```
T1:  base = subscription_cost / weekly_quota_tokens
T2:  base = cost_per_token (from provider API)
T3:  base = $0.001 (marginal cost = $0)
T4:  base = $0.001 (marginal cost = $0)
T5:  base = listed_rate (from provider catalog)
```

### Controller Multiplier
```
tracking_error = (ideal_usage - actual_usage) / capacity
                where ideal_usage = capacity × time_fraction_elapsed

multiplier = 1.0 - K_p × tracking_error

if time_fraction > 0.7:
    urgency = 1.0 - (1.0 - time_fraction) × 2.0
    multiplier *= (1.0 - K_u × (1.0 - urgency))

if utilization_projected > 1.0:
    overshoot = utilization_projected - 1.0
    multiplier *= (1.0 + overshoot × K_p)

multiplier = clamp(multiplier, 0.01, 10.0)
multiplier = 0.7 × multiplier + 0.3 × last_multiplier  (smoothing)
```

### Effective Price
```
effective_price = max(MIN_EFFECTIVE_PRICE, base_price × multiplier)
```

### Projected Usage
```
projected_usage = cumulative_usage + usage_rate × time_to_reset
                 + 0.5 × usage_acceleration × time_to_reset²
utilization_projected = projected_usage / capacity
```

### Profitability
```
effective_rate = subscription_cost / (actual_usage_tokens / 1_000_000)
value_score = tasks_completed × latency_score × reliability × model_quality_factor
profitability_ratio = value_score / subscription_cost
decision = f(effective_rate, tasks_completed, profitability_ratio)
```

---

## 11. Per-Tier Pricing Approach (Felix's Clarification)

Felix clarified that the LQG Kalman controller described in §2-3 is **ONLY for T1 (quota providers)**. Other provider types need different, simpler approaches — most of which are already implemented and correct as-is.

The key insight: the controller's objective (exhaust quota just before reset) only makes sense when there's a hard capacity limit with a known reset time. Providers without that structure need different mechanisms — or none at all.

### 11.1 T1 (quota — z.ai ours, friend): LQG CONTROLLER

- **Hard capacity** (weekly quota from API), known reset time
- **Objective**: exhaust quota just before reset (use-it-or-lose-it)
- **Kalman as CONTROLLER**: adjusts price multiplier to steer consumption
- **State**: `[burn_rate, velocity, acceleration]` from existing `ConsumptionKalman`
- **Control**: `price_multiplier = LQR(burn_rate, remaining_quota, time_to_reset)`
- **This is the ONLY tier where the controller runs**
- **Status**: NOT yet shipped — needs Phase 0-2 implementation (§7)
- **Current interim** (commit `7220cd3`): `$0.001 × max(0.0001, days_to_reset/7)` — sunk cost with time decay, no controller

### 11.2 T2 (balance — NeuralWatt $100/mo): TWO-PHASE STATE MACHINE

**Felix's clarification (2026-08-24)**:

> "we get a fixed amount of kwh to burn through for the entire month on neuralwatt,
> but we can always burn more at the same rates if we top up our balance, so we
> don't get a hard cutoff where we have to pay higher rates after burning through
> the fixed monthly amount"

This means NeuralWatt has **TWO phases**, not a single depletion-aware model:

#### Phase A — Prepaid (included kWh available)

- **Marginal cost = $0** (the $100/mo is a sunk cost; included kWh is already paid for)
- **Effective price = $0.001/M** (same as T3/T4 flat-rate floor)
- **NO depletion penalty** — using included kWh is free at the margin
- **NO rate increase as balance drops** — the included kWh is a sunk cost, not a depleting resource that should be "preserved"

#### Phase B — Pay-per-token (included kWh exhausted)

- **Marginal cost = measured per-token rate** (same rate as top-up, no increase)
- **Effective price = Kalman-measured $/M** (like T5 per-token)
- **NO rate increase** — Felix explicitly said "same rates", not higher rates
- **Transition trigger**: remaining_kWh ≤ 0 (from balance bridge API)

#### Why the current depletion penalty is WRONG

The current implementation (commit `db88e0f`):
```
effective = base × (1 + depletion_penalty) × 0.2762
depletion_penalty = (1 - remaining/initial) × 2.0  → price RISES as balance drops
```

This is incorrect because:
1. **It creates a vicious circle**: price rises → router avoids NeuralWatt → less usage → balance "preserved" but wasted (use-it-or-lose-it if kWh doesn't carry over)
2. **Felix said "no caps" and "same rates"** — the rate does NOT increase after exhaustion, so price should be STABLE, not rising
3. **The included kWh is a sunk cost** — marginal cost is $0 during Phase A. Penalizing usage of a free resource is backwards
4. **It discourages the exact behavior we want** — using the included kWh before it expires

#### The correction factor (0.2762) is MEASUREMENT ONLY

The 0.2762 correction factor compensates for NeuralWatt's API overcounting usage ~3.6×. It is a **measurement correction**, not a pricing adjustment:

- **Applies to COST TRACKING**: `_extract_cost()` and `_record_spend()` — what we record as actual spend
- **Does NOT apply to ROUTING PRICE**: the effective $/M the router sees should reflect REAL marginal cost
- In Phase A: $0.001 (correction irrelevant — it's the floor, marginal cost is $0)
- In Phase B: measured_rate from Kalman (the Kalman already incorporates the correction via corrected `_extract_cost()` observations)

#### kWh carry-over question (CONFIGURATION — unknown)

Whether unused kWh carries over to the next month is unknown. This must be a **configuration question during provider onboarding** (see Step 2.5 in the adding-api-key skill).

**Default assumption: does NOT carry over** (most prepaid plans don't).

If kWh does NOT carry over, Phase A gets **monthly time decay** (like T1 but with a monthly cycle instead of weekly):

```python
# Phase A with time decay (kWh does NOT carry over):
days_to_reset = days_to_monthly_reset  # 30 for monthly cycle
time_decay = max(0.0001, days_to_reset / 30)
effective = MIN_EFFECTIVE_PRICE * time_decay  # $0.001 × decay
# Same architecture as T1, different period (30 days vs 7 days)
```

If kWh DOES carry over, no time decay needed — use it whenever, it doesn't expire:
```python
# Phase A without time decay (kWh carries over):
effective = MIN_EFFECTIVE_PRICE  # $0.001 flat, like T3
```

#### Pseudocode: T2 state machine

```python
def compute_t2_price(provider, base_rate, context):
    remaining_kwh = _get_neuralwatt_remaining_kwh()

    if remaining_kwh > 0:
        # Phase A: prepaid kWh available — sunk cost, marginal $0
        if NW_KWH_CARRIES_OVER:
            # kWh doesn't expire → no urgency → flat floor
            return MIN_EFFECTIVE_PRICE  # $0.001
        else:
            # kWh expires monthly → use-it-or-lose-it time decay
            days_to_reset = _days_to_monthly_reset()
            time_decay = max(0.0001, days_to_reset / 30.0)
            return MIN_EFFECTIVE_PRICE * time_decay
    else:
        # Phase B: included kWh exhausted — pay per token at same rate
        # The Kalman measures actual $/M from corrected cost observations
        return max(MIN_EFFECTIVE_PRICE, base_rate)
```

#### What changes from the current implementation

| Aspect | Old (db88e0f) | New (this design) |
|--------|---------------|-------------------|
| Phase A pricing | `base × (1 + penalty) × 0.2762` (rises with depletion) | `$0.001` (or `$0.001 × time_decay` if no carry-over) |
| Phase B pricing | Same formula (just higher penalty) | `measured_rate` (Kalman, like T5) |
| Depletion penalty | `(1 - remaining/initial) × 2.0` | **REMOVED** — no penalty for using prepaid kWh |
| Correction factor (0.2762) | Applied to routing price | Applied to cost tracking only (`_extract_cost`) |
| State model | Single formula, price rises monotonically | Two-phase state machine with transition at kWh = 0 |
| Carry-over | Not considered | Configuration question (default: no carry-over → time decay) |

**Status**: DESIGN ONLY — needs implementation to replace `_compute_depletion_penalty()` and the T2 branch of `compute_effective_price()`

### 11.3 T3 (flat-rate — opencode_go $10/mo): STATIC FLOOR

- **Unlimited (or unknown capacity)**, marginal cost $0
- `effective = $0.001` always (when healthy)
- **No controller, no time decay, no balance tracking**
- **Health drops on 429/rate-limit** — that's the only signal that matters
- **Already implemented** (commit `db88e0f`) — correct as-is

### 11.4 T4 (included — ollama_cloud): STATIC FLOOR

- Same as T3. Marginal cost $0, included in subscription.
- `effective = $0.001` always (when healthy)
- **Already implemented** (commit `db88e0f`) — correct as-is

### 11.5 T5 (per-token — routstr, openrouter, deepinfra, ppq, telnyx): KALMAN OBSERVER

- **No capacity, no reset, no balance**
- **Price = measured actual cost per token** from traffic
- **Kalman as OBSERVER** (current behavior) — tracks real $/M
- **No controller needed** — no capacity to exhaust
- **Already implemented** (commit `db88e0f`) — correct as-is

### 11.6 Summary Table

| Tier | Provider | Approach | Controller? | Already Shipped? |
|------|----------|----------|-------------|-----------------|
| T1 | z.ai | LQG controller | YES | No — needs Phase 0-2 |
| T2 | NeuralWatt | Two-phase state machine (Phase A prepaid, Phase B per-token) | No | DESIGN ONLY — replaces db88e0f depletion penalty |
| T3 | opencode_go | Static $0.001 floor | No | Yes (db88e0f) |
| T4 | ollama_cloud | Static $0.001 floor | No | Yes (db88e0f) |
| T5 | routstr, etc. | Kalman observer | No | Yes (db88e0f) |

### 11.7 Conclusion

**Only T1 needs the new LQG controller.** T3-T5 are already correctly implemented with tonight's work (commit `db88e0f`). T2 (NeuralWatt) needs a redesign based on Felix's clarification — the depletion penalty is being replaced by a two-phase state machine (§11.2). The transition plan (Phase 0-2, §7) should be scoped to **T1 only**, with T2 as a separate design item.

This simplifies the implementation significantly:
- No LQG controller needed for T2 — just a state machine with two branches (prepaid vs per-token)
- No LQG controller needed for T3/T4 (static floor is correct — unlimited or unknown capacity)
- No LQG controller needed for T5 (observer-only is correct — no capacity to exhaust)
- The `CapacityController` class (§7.3) only needs to handle the T1 case
- The profitability tracker (§5) still applies to all subscription providers (T1-T4)

**Open Question §11.7 (old #7)**: The T2 "maximize value per token" question is now answered differently: NeuralWatt is a two-phase model (§11.2). In Phase A, marginal cost is $0 (sunk cost) — the router should prefer NeuralWatt aggressively. In Phase B, it's per-token cost — the router competes on measured rate. The depletion penalty was wrong because it made Phase A progressively MORE expensive (discouraging usage of a free resource). The correct model is: cheap in Phase A, measured rate in Phase B, NO transition penalty.

---

## 12. External Sell Pricing (Dual Pricing Surface)

### 12.1 The Problem: Internal Routing Price ≠ External Sell Price

Felix's concern (2026-08-24):

> "There is a risk that we run at a loss for an entire month if we expose these prices on routstr. Expose these prices to our live router which always chooses the cheapest endpoint, but lets make sure that the prices that the live router exposes to real users on routstr are high enough to ensure that we always make a profit when selling the tokens to a third party over routstr."

The capacity-aware pricing model in this doc (§1–§11) defines **internal routing prices** — the prices `select_provider()` uses to pick the cheapest upstream provider. These prices are **artificially low** for sunk-cost providers:

- T1 (z.ai): $0.001/M × time_decay — as low as $0.000006/M near quota reset
- T2 (NeuralWatt Phase A): $0.001/M — prepaid kWh treated as sunk cost
- T3 (opencode_go): $0.001/M — $10/mo flat, marginal cost $0
- T4 (ollama_cloud): $0.001/M — included in subscription, marginal cost $0
- T5 (per-token): Kalman-measured actual rate

**These routing prices are CORRECT for internal routing** — we want to attract traffic to sunk-cost providers. But they are **WRONG for external sell prices**. If we charge a third party $0.001/M for z.ai tokens, we lose money on the subscription cost because $0.001/M doesn't cover the real amortized cost ($20/mo ÷ monthly quota).

**Solution: Two independent price surfaces.**

### 12.2 The Two Price Surfaces

```
                    ┌──────────────────────────────────┐
                    │         PROVIDER TIER            │
                    │  (T1-T5 economic classification) │
                    └────────────┬─────────────────────┘
                                 │
                ┌────────────────┴────────────────┐
                │                                  │
                ▼                                  ▼
  ┌──────────────────────┐          ┌──────────────────────┐
  │  SURFACE 1: INTERNAL  │          │  SURFACE 2: EXTERNAL │
  │    ROUTING PRICE      │          │    SELL PRICE        │
  ├──────────────────────┤          ├──────────────────────┤
  │ Used by:              │          │ Used by:              │
  │  flat_router.py       │          │  routstr API          │
  │  select_provider()    │          │  (sats/token rate     │
  │  → cheapest wins      │          │   exposed to third     │
  │                       │          │   parties)             │
  │ Formula:              │          │ Formula:               │
  │  compute_effective_   │          │  compute_sell_price()  │
  │  price() [§1-§11]     │          │  [§12.4 below]        │
  │                       │          │                        │
  │ Purpose:              │          │ Purpose:               │
  │  Attract traffic to   │          │  Charge third parties  │
  │  sunk-cost providers  │          │  at REAL cost + margin │
  │  (artificially low)   │          │  (always profitable)    │
  │                       │          │                        │
  │ Exposed to:           │          │ Exposed to:            │
  │  INTERNAL ONLY        │          │  EXTERNAL USERS        │
  │  (never on routstr)   │          │  (on routstr only)     │
  └──────────────────────┘          └──────────────────────┘
```

**Key invariant**: The external sell price is ALWAYS ≥ actual cost × (1 + profit_margin). The internal routing price can be below actual cost (it's a sunk-cost optimization, not a billing rate).

### 12.3 Actual Cost per Tier (for Sell Pricing)

The "actual cost" for sell pricing is **different** from the routing price. It reflects what we actually pay per token, not the artificial routing incentive.

| Tier | Provider | Actual Cost Formula | Notes |
|------|----------|---------------------|-------|
| T1 (quota) | z.ai ours, z.ai friend | `subscription_cost / monthly_quota_tokens` | E.g., $20/mo ÷ 200M × 4.33 = $0.023/M. This is the amortized real cost. |
| T2 (balance) | NeuralWatt | Phase A: `subscription_cost / typical_monthly_usage`<br>Phase B: `measured_rate` (Kalman) | Phase A: $100/mo ÷ historic usage. Phase B: per-token rate when kWh exhausted. |
| T3 (flat) | opencode_go | `subscription_cost / historic_avg_monthly_usage` | $10/mo ÷ expected usage. If no history: conservative estimate (e.g., 50M/mo → $0.20/M). |
| T4 (included) | ollama_cloud | `subscription_cost / historic_avg_monthly_usage` | Included in subscription — allocate subscription cost proportionally. |
| T5 (per-token) | routstr, ppq, deepinfra, openrouter, telnyx | `measured_rate` (from Kalman filter) | Straightforward — we pay per token, sell at markup. |

**Important**: For T1-T4, the "actual cost" is based on subscription economics, NOT marginal cost. Marginal cost is $0 (sunk cost) — but the actual cost per token is NOT $0, because we paid a subscription fee. The sell price must recover the subscription cost plus margin.

### 12.4 Sell Price Formula

```python
def compute_sell_price(
    provider: str,
    model: str | None,
    context: dict | None = None,
) -> float:
    """Compute the external sell price ($/M) for a provider.

    This is the price we charge third parties on routstr.
    It is ALWAYS >= actual_cost × (1 + min_profit_margin).

    This is SEPARATE from compute_effective_price() which is the
    internal routing price used by select_provider().
    """
    tier = PROVIDER_TIER.get(provider, "per_token")
    margin = PROFIT_MARGIN.get(provider, DEFAULT_PROFIT_MARGIN)  # 0.20

    actual_cost = _compute_actual_cost(provider, tier, model, context)

    sell_price = actual_cost * (1.0 + margin)

    # Safeguard: never sell below minimum margin
    min_sell = actual_cost * (1.0 + MIN_PROFIT_MARGIN)  # 1.1x
    return max(sell_price, min_sell)
```

### 12.5 Profit Margin Configuration

```python
# Default profit margins per provider (configurable)
DEFAULT_PROFIT_MARGIN = 0.20   # 20% — standard markup
MIN_PROFIT_MARGIN = 0.10       # 10% — hard floor, never sell below 1.1x cost

PROFIT_MARGIN: dict[str, float] = {
    # T1: quota providers — standard margin
    "ours":         0.20,
    "friend":       0.20,

    # T2: NeuralWatt — standard margin (premium models get more)
    "neuralwatt":   0.20,

    # T3: opencode_go — slightly higher (unknown capacity = risk premium)
    "opencode_go":  0.30,

    # T4: ollama_cloud — standard
    "ollama_cloud": 0.20,

    # T5: per-token — thin margin (competitive market)
    "routstr":      0.15,
    "routstrd":     0.15,
    "deepinfra":    0.15,
    "ppq":          0.20,
    "openrouter":   0.15,
    "telnyx":       0.20,

    # Premium models can override:
    # "neuralwatt:kimi-k3": 0.50,
}
```

**Rules:**
- Default margin: 20% (configurable per provider)
- Hard minimum: 10% — `sell_price ≥ actual_cost × 1.1` always
- Premium models (e.g., kimi-k3, heavy reasoning): up to 50% margin
- Per-token providers in competitive markets: 15% (thin margin to stay competitive)

### 12.6 Determining "Actual Cost" for Sell Pricing

The `_compute_actual_cost()` function determines the real cost per token for sell pricing:

```python
def _compute_actual_cost(
    provider: str,
    tier: str,
    model: str | None,
    context: dict | None = None,
) -> float:
    """Determine the actual cost per token ($/M) for sell pricing.

    This is DIFFERENT from the routing price. It reflects real
    subscription economics, not sunk-cost optimization.
    """
    if tier == "quota":
        # T1: subscription_cost / monthly_quota_tokens
        # E.g., $20/mo ÷ (200M × 4.33 weeks) = $0.023/M
        sub_cost = SUBSCRIPTION_COST.get(provider, 20.0)  # $/mo
        monthly_quota = _get_monthly_quota_tokens(provider)  # tokens
        if monthly_quota > 0:
            return sub_cost / (monthly_quota / 1_000_000)  # $/M
        # Fallback: no quota known
        return max(_get_measured_rate(provider, model), FALLBACK_RATE)

    elif tier == "balance":
        # T2: Phase A → sub_cost / typical_monthly_usage
        #     Phase B → measured_rate (Kalman)
        if _is_neuralwatt_phase_b(provider):
            return _get_measured_rate(provider, model)  # per-token
        else:
            sub_cost = SUBSCRIPTION_COST.get(provider, 100.0)
            typical_usage = _get_historic_monthly_usage(provider)  # tokens
            if typical_usage > 0:
                return sub_cost / (typical_usage / 1_000_000)
            return max(_get_measured_rate(provider, model), FALLBACK_RATE)

    elif tier == "flat":
        # T3: sub_cost / historic_avg_monthly_usage
        sub_cost = SUBSCRIPTION_COST.get(provider, 10.0)
        avg_usage = _get_historic_monthly_usage(provider)
        if avg_usage > 0:
            return sub_cost / (avg_usage / 1_000_000)
        # No history → conservative estimate
        return sub_cost / (CONSERVATIVE_USAGE_ESTIMATE / 1_000_000)

    elif tier == "included":
        # T4: same as T3 — allocate subscription cost
        sub_cost = SUBSCRIPTION_COST.get(provider, 0.0)  # may be $0
        if sub_cost == 0:
            # Truly free — use a nominal rate
            return FALLBACK_RATE
        avg_usage = _get_historic_monthly_usage(provider)
        if avg_usage > 0:
            return sub_cost / (avg_usage / 1_000_000)
        return FALLBACK_RATE

    else:
        # T5: per-token — measured rate from Kalman
        measured = _get_measured_rate(provider, model)
        if measured > 0:
            return measured
        return FALLBACK_RATE
```

**Constants:**

```python
FALLBACK_RATE = 0.50          # $/M — used when actual cost can't be determined
CONSERVATIVE_USAGE_ESTIMATE = 50_000_000  # 50M tokens/month — conservative for flat-rate

SUBSCRIPTION_COST: dict[str, float] = {
    "ours":         20.0,   # z.ai subscription $20/mo
    "friend":       0.0,    # friend's key — we don't pay for it
    "neuralwatt":  100.0,   # NeuralWatt $100/mo
    "opencode_go": 10.0,    # opencode.go $10/mo
    "ollama_cloud": 0.0,    # included in other subscription
    # T5 providers: no subscription (pay per token)
}
```

### 12.7 Safeguards

1. **Unknown cost fallback**: If actual_cost cannot be determined (no historic usage, no known quota):
   ```
   sell_price = max(measured_rate, FALLBACK_RATE) × (1 + margin)
   ```
   The `FALLBACK_RATE` ($0.50/M) is deliberately conservative — it's better to overcharge than to lose money.

2. **Never-used provider**: If a provider has never been used (no Kalman signal, no history):
   ```
   sell_price = CONSERVATIVE_ESTIMATE × (1 + margin)
   ```

3. **Minimum margin enforcement**: The sell price is clamped to `≥ actual_cost × 1.1`. No configuration can override this floor. If the margin config is set to 5%, the actual margin applied is 10% (the minimum).

4. **Monthly profitability check** (alert, not block):
   ```
   At end of each billing period:
     For each provider:
       sell_revenue = sum(sell_price × tokens_sold) for all routstr traffic
       actual_cost_total = subscription_cost + per_token_costs
       if sell_revenue < actual_cost_total:
         ALERT("Provider {X} ran at a loss: revenue={rev}, cost={cost}")
   ```
   This is a reporting metric, not a blocking mechanism. If a provider consistently runs at a loss, the margin should be increased or the provider removed from routstr.

5. **Friend's key (zero subscription cost)**: For `friend` (z.ai friend's key), `subscription_cost = $0`. The actual_cost is $0, so `sell_price = $0 × (1 + margin) = $0`. This is correct — we don't pay for it, so we can sell it at any price. But for external pricing consistency, we should still charge at least the `FALLBACK_RATE` to avoid undercutting our own paid providers. The sell price for zero-cost providers is:
   ```
   sell_price = max(FALLBACK_RATE, actual_cost) × (1 + margin)
   ```

### 12.8 Implementation Plan

**DESIGN ONLY — no code changes yet.**

When implemented:

1. **New module**: `sell_pricing.py` (or function in `flat_router.py`)
   - `compute_sell_price(provider, model, context)` — the external sell price
   - `_compute_actual_cost(provider, tier, model, context)` — actual cost lookup
   - Constants: `PROFIT_MARGIN`, `SUBSCRIPTION_COST`, `FALLBACK_RATE`

2. **Routstr integration**: The routstr API endpoint exposes `compute_sell_price()` as the sats/token rate. The internal `compute_effective_price()` is never exposed.

3. **No change to flat_router.py routing**: `select_provider()` continues to use `compute_effective_price()` (Surface 1). The sell price (Surface 2) is computed independently and only used for billing.

4. **Monthly profitability report**: A cron job queries `api_calls` for routstr traffic per provider, computes `sell_revenue` vs `actual_cost`, and alerts if any provider ran at a loss.

### 12.9 Relationship to §5 (Profitability Tracking)

The profitability tracker in §5 tracks actual usage vs subscription cost for **monthly renewal decisions** (should we keep paying for this subscription?). The sell price safeguard in §12.7 tracks whether we're **charging enough** on routstr.

These are complementary:
- §5 answers: "Is this subscription worth keeping?" (cost vs value)
- §12 answers: "Are we charging enough to cover our costs?" (revenue vs cost)

Both use the same `subscription_cost` and `historic_usage` data, but for different purposes.

### 12.10 Summary

| Aspect | Surface 1 (Internal Routing) | Surface 2 (External Sell) |
|--------|-------------------------------|---------------------------|
| Function | `compute_effective_price()` | `compute_sell_price()` |
| Used by | `select_provider()` (flat router) | routstr API (billing) |
| Formula | Tier-specific (§1-§11) | actual_cost × (1 + margin) |
| T1 price | $0.001 × time_decay (as low as $0.000006) | $0.023/M × 1.2 = $0.028/M |
| T3 price | $0.001/M | $0.20/M × 1.3 = $0.26/M (if 50M/mo usage) |
| T5 price | measured_rate | measured_rate × 1.15 |
| Purpose | Attract traffic to sunk-cost | Always profitable |
| Exposed to | Internal only | External (routstr) |
| Can be below cost? | YES (sunk cost optimization) | NO (minimum 1.1× actual cost) |

---

## 13. Open Questions for Felix

1. **T3/T4 controller**: Should we run the controller on opencode_go and ollama_cloud, or treat them as unlimited ($0.001 floor)? My recommendation: ollama_cloud YES (has known quota), opencode_go NO (treat as unlimited until we see throttling).

2. **Control period**: 5 minutes is my recommendation. Is that too slow for z.ai (5-hour quota window)? For the 5h window, 5 minutes gives 60 control steps — probably enough.

3. **Elasticity model**: Should we try to estimate traffic elasticity (α in the logarithmic model) from historical data, or stick with the linear approximation? My recommendation: linear first, upgrade if oscillation is a problem.

4. **Value score**: Is the composite metric (tasks × latency × reliability × quality) the right measure of "value"? Or should it be simpler (just tokens served)?

5. **Decision thresholds**: The profitability decision thresholds ($5/M → CANCEL, $1/M + <50 tasks → DOWNGRADE) are my initial guesses. These should be tuned after the first month of data.

6. **Per-model profitability**: Should the profitability report break down by model within each provider? (e.g., "opencode_go served 400M tokens of glm-5.2 and 87M tokens of kimi-k3")? This would require joining `api_calls` on model.

7. **Controller for NeuralWatt (T2)**: ~~The controller's objective for T2 is different from T1. For T1, we want to exhaust the quota (it's free). For T2, we want to *not waste* the balance (it's prepaid, but we don't want to burn it on low-value tasks). Should the T2 controller optimize for "maximize value per token" rather than "exhaust balance by period end"?~~ **ANSWERED in §11.2**: NeuralWatt is a TWO-PHASE state machine, not a depletion model. Phase A (prepaid kWh): marginal cost $0, price = $0.001 (or $0.001 × monthly time_decay if kWh doesn't carry over). Phase B (kWh exhausted): price = measured rate. The depletion penalty was WRONG — it penalized using a sunk-cost resource. The correction factor (0.2762) is measurement-only (cost tracking), not a routing price modifier.

---

**End of design doc. Felix reviews before any implementation.**