# Pricing Model Plan — Summary

**Date:** 2026-08-24
**Authors:** Felix + Hermes Manager
**Status:** T1/T3/T4/T5 implemented, T2 design complete (implementation pending), dual pricing surface design only

---

## 1. The Problem

Three critical issues were discovered on 2026-08-24:

1. **Cost inflation 15.7×** — The cost-escalation alert fired at $33.71/h. Real cost was ~$0.43/h. Root causes:
   - NeuralWatt 3.6× overcounting correction (0.2762) not applied in `_extract_cost()` (only in `_estimate_cost_usd()`)
   - opencode_go recorded at $0.43/M but real marginal cost is $0 ($10/mo flat rate)
   - ollama_cloud same issue — included in subscription, marginal $0, but recorded $0.29/h

2. **Wrong routing** — Flat router sent deepseek-v4-flash traffic to NeuralWatt ($1.43/M) instead of opencode_go ($0 marginal). Two-part bug: early-exit path fired for all `deepseek/*` models before the flat router, and `_try_opencode_go()` sent the raw model name instead of the bare name opencode.ai expects.

3. **Invisible burn** — routstr burned 1.25M tokens with 100% NULL `cost_usd`. `_extract_cost()` had branches for some providers but not routstr, routstrd, deepinfra, ppq, openrouter. Additional: ppq 22 calls 100% NULL (response format changed), opencode_go 33% NULL (streaming SSE passes total_tokens=0), ollama_cloud_2 99% NULL (same streaming issue).

**Underlying design flaw:** The original pricing model used `base_price = subscription_cost / actual_usage`, creating a vicious circle: underused provider → fewer tokens → higher base price → router avoids it → even fewer tokens → price rises more.

---

## 2. The Solution

### 5-Tier Provider Classification

| Tier | Providers | Economic Model | Pricing Approach |
|------|-----------|----------------|-------------------|
| T1 (quota) | z.ai ours, z.ai friend | Weekly quota, use-it-or-lose-it, resets Aug 27 | Sunk cost + time decay; LQG controller (future) |
| T2 (prepaid+per-token) | NeuralWatt | Monthly kWh included, top up at same rate | Two-phase state machine |
| T3 (flat-rate) | opencode_go | $10/mo, unlimited/unknown capacity | $0.001/M static floor |
| T4 (included) | ollama_cloud, ollama_cloud_2 | Included in subscription | $0.001/M static floor |
| T5 (per-token) | routstr, routstrd, deepinfra, ppq, telnyx, openrouter | Pure pay-per-token | Kalman observer (measures real $/M) |

### Key Formulas

- **T1:** `effective = $0.001 × max(0.0001, days_to_reset / 7)` — sunk cost with time decay. Cheapest when quota is freshest, drops as reset approaches. No conservation penalty.
- **T2:** Two-phase: Phase A (prepaid kWh available) = $0.001/M (or $0.001 × monthly time decay if kWh doesn't carry over). Phase B (kWh exhausted) = Kalman-measured per-token rate. Transition at remaining_kWh ≤ 0.
- **T3/T4:** `effective = $0.001/M` always. Health drops on 429/rate-limit.
- **T5:** `effective = measured_rate` (from Kalman filter). Observer only, no controller.
- **Catch-all fallback:** Any provider without a specific `_extract_cost()` branch gets `cost = _rpt_rate(provider) × tokens`. Prevents future invisible burn.

### Dual Pricing Surface

**Surface 1 (Internal Routing Price):** `compute_effective_price()` — artificially low for sunk-cost providers. Used by `select_provider()` to pick the cheapest upstream. Never exposed externally.

**Surface 2 (External Sell Price):** `compute_sell_price()` (design only) — always charges above real cost + profit margin. Used by routstr API for third-party billing. Formula: `actual_cost × (1 + margin)` where margin defaults to 20%, hard minimum 10%.

| | Surface 1: Internal | Surface 2: External |
|---|---|---|
| T1 example | $0.001 × time_decay (as low as $0.000006/M) | $0.023/M × 1.2 = $0.028/M |
| T3 example | $0.001/M | $0.20/M × 1.3 = $0.26/M (if 50M/mo usage) |
| T5 example | measured_rate (Kalman) | measured_rate × 1.15 |
| Can be below cost? | YES — sunk cost optimization | NO — minimum 1.1× actual cost |

---

## 3. What's Implemented (LIVE)

| Component | Commit |
|-----------|--------|
| Flat router (12 providers, select_provider) | `a6b086e` |
| T1 sunk cost + time decay | `7220cd3` |
| T2 depletion penalty (WRONG — needs replacement) | `db88e0f` |
| T3/T4 $0.001 floor | `db88e0f` |
| T5 Kalman observer | `db88e0f` |
| NeuralWatt correction in _extract_cost | `dcb648c` |
| deepseek model translation in _try_opencode_go | `a346900` |
| Catch-all cost extraction fallback | `a41ed60` |
| Provider onboarding Step 2.5 (pricing questions) | `fd61800` (skill) |

---

## 4. What's Design Only (Not Yet Implemented)

| Component | Status | Notes |
|-----------|--------|-------|
| T2 two-phase state machine | DESIGN ONLY | Replaces depletion penalty (commit `7fb481f` was design work). Phase A = $0.001 prepaid, Phase B = measured rate. |
| T1 LQG controller | DESIGN ONLY | Closed-loop controller to replace open-loop time decay. Kalman estimator + LQR control law. Only tier where controller makes sense. |
| Profitability tracking table | DESIGN ONLY | `subscription_profitability` table for monthly renewal decisions. Reporting only, not a pricing input. |
| External sell pricing (Surface 2) | DESIGN ONLY | `compute_sell_price()` function, routstr API integration, monthly profitability check. See handover §8, design doc §12. |

---

## 5. Known Issues Remaining

1. **Streaming SSE total_tokens=0** — Streaming responses pass `total_tokens=0` to `_extract_cost`, causing 33% NULL for opencode_go and 99% NULL for ollama_cloud_2. Root cause of remaining invisible burn.

2. **routstr_probe.py broken** — Daily cost probe fails: routstr returns 401 (stale key), routstrd returns 405 (daemon API changed). No measured rates for the Kalman to learn from for T5 providers.

3. **ppq response format changed** — `cost_extraction.py` has a ppq entry but it stopped working. 22 calls, 100% NULL.

4. **T2 depletion penalty still in live code** — The two-phase state machine is designed but not implemented. Current behavior penalizes NeuralWatt usage as balance drops (wrong — creates vicious circle).

5. **z.ai quota locked** — Resets Aug 27. T1 formula is correct but inactive until quota resets.

6. **opencode_go key health** — Was 401ing earlier today. After subscription activation, key started working. Health recovery may not be complete — check `key_health` table.

---

## 6. Next Steps (Prioritized)

| Priority | Task | Impact | Effort |
|----------|------|--------|--------|
| P0 | **Implement T2 two-phase state machine** — replace depletion penalty with prepaid kWh → per-token transition | High: NeuralWatt becomes cheap while kWh available | Medium |
| P0 | **Fix streaming SSE total_tokens=0** — root cause of remaining invisible burn for opencode_go (33% NULL) and ollama_cloud_2 (99% NULL) | High: eliminates invisible burn | Medium |
| P1 | **Fix routstr_probe.py** — 401 (stale key) and 405 (daemon API changed) | Medium: Kalman can't converge for T5 without measured rates | Low |
| P1 | **Fix ppq cost extraction** — response format changed, 22 calls 100% NULL | Medium: eliminates another invisible burn source | Low |
| P1 | **Verify opencode_go key health** — ensure 401 failures fully recovered | Medium: correct routing of deepseek traffic | Low |
| P2 | **Implement external sell pricing (Surface 2)** — `compute_sell_price()` function, routstr API integration | High: prevents running at a loss on routstr | Medium |
| P2 | **Phase 0: Capacity tracking table** — `subscription_profitability` table, monthly report | Low: data collection for renewal decisions | Low |
| P3 | **Phase 1-2: T1 LQG controller** — replace open-loop time decay with closed-loop controller | Medium: fine-tunes quota burn rate | High |

---

## 7. Design Documents

- `docs/pricing-handover-2026-08-24.md` — Complete handover document (this plan is a summary)
- `~/.hermes/profiles/manager/state/capacity-aware-pricing-design.md` — LQG controller + profitability tracking + external sell pricing (1674 lines)
- `~/.hermes/profiles/manager/state/time-aware-pricing-design.md` — Original 5-tier pricing design
- `~/.hermes/profiles/manager/state/invisible-burn-analysis.md` — routstr invisible burn analysis (24KB)
- `~/.hermes/profiles/manager/skills/devops/adding-api-key-to-live-router/SKILL.md` — 11-step + Step 2.5 onboarding skill

---

## 8. Key Decisions Log

1. **Sunk cost providers get $0.001/M floor** (not $0) — avoids "always wins" edge case where router sends ALL traffic to one provider.
2. **T1 time decay applies to the $0.001 floor, not the base_rate** — z.ai is cheapest at start of week (quota fresh), drops as reset approaches. Previous formula was backwards.
3. **T2 depletion penalty is WRONG** — Felix confirmed NeuralWatt has no rate increase after kWh exhaustion. Prepaid kWh is a sunk cost (marginal $0). Penalty creates vicious circle.
4. **NeuralWatt 0.2762 correction is measurement-only** — applies to cost tracking (`_extract_cost`), NOT routing price. Router sees real marginal cost.
5. **Dual pricing surface** — Internal routing price (artificially low for sunk cost) is separate from external sell price (always above real cost + margin). Prevents running at a loss on routstr.
6. **LQG controller is T1-only** — Only quota providers have hard capacity + known reset time. T3/T4 (unlimited/included) and T5 (per-token) don't need a controller.
7. **Profitability tracking is decoupled from pricing** — `subscription_profitability` table is a reporting metric for monthly renewal decisions, NOT a pricing input. This breaks the vicious circle.
8. **Catch-all cost extraction** — Any provider without a specific `_extract_cost()` branch gets `cost = _rpt_rate(provider) × tokens`. Safety net against future invisible burn.