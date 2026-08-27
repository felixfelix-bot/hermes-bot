# ADR-007: Gated Rollout of Excess z.ai Quota Sales on Routstr

**Status:** Accepted — GATES CLOSED (do not sell until all gates pass)
**Date:** 2026-08-27
**Supersedes:** None
**Related:** ADR-005 (dual pricing surface), flat-router-design.md, kalman convergence tooling

---

## Decision

We will NOT sell excess z.ai quota on routstr until ALL four gates below have
been passed and verified. This ADR exists to prevent premature selling (which
risks exhausting our own quota) and to ensure we're alerted when selling becomes
safe (so we don't waste sellable quota).

## Context

Felix's z.ai proxy manages two API keys with 5-hour, weekly, and monthly quota
windows. When quota is predicted to be excess at window end, it could be sold
on routstr (Felix's Cashu-paywalled inference node) for sats.

**Current blocker:** The Kalman filter predicting quota exhaustion has
**MAPE = 118.32%** (retuned 2026-08-27T18:20:53Z). This is worse than random —
predictions are wrong by more than the actual value on average. We cannot
determine what is truly "excess" vs "needed."

Root causes of filter divergence:
1. Quota resets introduce discontinuities a linear Kalman cannot model
2. Burn rate is highly variable (idle periods mixed with heavy cron bursts)
3. Both keys modeled with same approach despite different usage patterns
4. Measurement noise estimate is astronomically high (4.2×10¹³)

## The Four Gates (ALL must pass before selling)

### Gate 1: Prediction Accuracy

- Kalman MAPE < 15% sustained for **7 consecutive days**
- Per-key, per-window-type (5h/weekly/monthly) accuracy measured separately
- Verified by: `python3 ~/.hermes/bot/kalman_health.py` reporting convergence
- Requires: redesign of the filter to model resets explicitly (piecewise per
  window, not as process noise)

**If this gate fails:** Predictions are unreliable. Any "excess" calculation
is guesswork. Do not sell.

### Gate 2: Priority Routing

- Flat router must distinguish internal vs sold-to-customer requests
- Internal requests (Hermes agents, kanban workers, crons) are ALWAYS served
- Sold requests are served only when prediction says "safe" (hours-to-exhaustion
  > threshold)
- When threshold crossed: sold requests get HTTP 429 + Retry-After header,
  internal requests unaffected
- Implementation: `X-Priority: internal|sold` header, checked against Kalman
  prediction before routing

**If this gate fails:** A customer request could consume quota our agents need.

### Gate 3: Stress Test

- Simulate z.ai at 90% quota exhaustion with mixed internal + sold load
- Verify: internal requests never blocked, sold requests degrade gracefully
- Verify: Kalman predicts exhaustion within ±2 hours of actual
- Verify: dynamic delisting triggers within 5 minutes of threshold crossing
- Run for minimum 24h continuous

**If this gate fails:** The system doesn't work under pressure.

### Gate 4: Dynamic Delisting

- Routstr listing can be pulled (delisted) programmatically within 5 minutes
- Trigger conditions: burn rate accelerates (predicted exhaustion moves forward),
  Kalman variance increases beyond threshold, or manual override
- Script exists and is tested: `~/.hermes/bot/scripts/routstr_delist.py`

**If this gate fails:** We can't react to changing conditions.

## Architecture (when all gates pass)

```
[z.ai keys] ──▶ [Flat Router] ──▶ [Kalman Predictor (per window)]
                    │                       │
              Priority routing         Excess Calculator:
              1. internal (always)     sellable = projected_eom_quota
              2. sold (if safe)                    − safety_margin
                    │                       │
                    ▼                       ▼
              [Routstr Node] ◀──── (list/delist dynamically)
              Prices excess z.ai tokens in sats
              via Cashu ecash
```

## Safety Margins (phased rollout)

| Phase | Sell fraction of computed excess | Safety margin | Duration |
|-------|--------------------------------|---------------|----------|
| 1 (pilot) | 50% | 30% of projected remaining | 1 week |
| 2 (cautious) | 70% | 25% | 2 weeks |
| 3 (normal) | 85% | 20% | ongoing |

Phase advancement requires: zero incidents of internal-request starvation +
prediction accuracy maintained + no emergency delisting needed.

## Surfacing Readiness (how we know when to start)

A monitoring cron (`routstr-readiness-check`, proposed) should:

1. Check all four gates daily
2. Report: which gates pass, which fail, by how much
3. When ALL gates pass for the first time: **ALERT Felix on Signal** —
   "Routstr selling is now safe. Gate 1 ✓ (MAPE 12.3%, 7 days), Gate 2 ✓
   (priority routing active), Gate 3 ✓ (stress test passed), Gate 4 ✓
   (delist tested). Recommend starting Phase 1 (50% of excess)."
4. Until then: stay silent unless a gate REGRESSES (was passing, now failing)

This prevents two failure modes:
- **Premature selling** (gates not passed → do not list)
- **Wasted quota** (gates passed but nobody notices → sellable quota expires unused)

## Consequences

### Positive
- Monetizes excess z.ai quota that currently expires unused
- Cashu payment integration (routstr already handles this)
- Kalman improvement benefits ALL routing decisions, not just selling

### Negative
- Kalman redesign is non-trivial (reset-aware filtering)
- Priority routing adds complexity to the flat router
- Customer requests may be rejected during our high-usage periods (managed by
  429 + Retry-After)

### Risks
- **Worst case:** We sell quota, our usage spikes, we exhaust quota. Mitigated
  by: priority routing (Gate 2) + dynamic delisting (Gate 4) + safety margins
- **Moderate case:** Predictions improve but aren't perfect. Mitigated by:
  20-30% safety margin absorbs prediction error
- **No risk case:** Gates never pass. We lose nothing (quota expires unused
  as it does today)

## Implementation Tasks (in dependency order)

1. **Kalman redesign** — reset-aware filtering, per-window, per-key.
   Target: MAPE < 15%. Timeline: ~1 week of clean data.
2. **Priority routing** — X-Priority header in flat router + prediction check.
   Timeline: 2-3 days.
3. **Stress test harness** — simulated quota pressure, mixed load.
   Timeline: 2-3 days.
4. **Routstr delist script** — programmatic listing removal.
   Timeline: 1 day.
5. **Readiness monitoring cron** — daily gate check + Signal alert when ready.
   Timeline: 1 day.

Total: ~2-3 weeks to readiness IF the Kalman redesign works on first attempt.

## References

- Current Kalman state: `~/.hermes/bot/kalman_tuning.json` (MAPE 118.32%, 2026-08-27)
- Flat router design: `~/.hermes/bot/docs/flat-router-design.md`
- Dual pricing surface: ADR-005
- Routstr operations: `routstr-node-ops` skill
- Kalman dashboard: https://kalman.tollgate.me (if deployed)

---

**HOLD DIRECTIVE:** No routstr listing of z.ai quota until this ADR's gates
are marked as PASSED by the readiness monitor AND Felix gives explicit approval.
This ADR prevents premature selling. The readiness monitor prevents wasted quota.
Both must coexist.
