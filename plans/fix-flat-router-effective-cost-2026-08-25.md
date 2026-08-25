# Plan: Fix Flat Router Effective Cost and Routing Inefficiency

**Date:** 2026-08-25
**Status:** DONE — all 5 phases implemented, verified, committed

## Problem Summary

The flat router is making wrong routing decisions due to incorrect effective cost calculations:

1. **PriceKalman Initialization Bug** — The shadow optimizer initializes ollama_cloud with $0.40/M (old effective price) instead of $0.001/M (new effective price for T4 providers). This causes the flat router to think ollama_cloud is 400x more expensive than it actually is.

2. **Routstrd Being Chosen for glm-4.5-flash** — The flat router is choosing routstrd ($1.00/M estimated, actually $6.8/M) for glm-4.5-flash when ollama_cloud can serve it for $0.001/M. This is causing $12.99/hour to be spent on routstrd (475 calls, 55.7s average duration).

3. **Kalman Composition Alert** — The Kalman filter predicts $0.1737/hour based on 373368 tokens/h × $0.47/M (old z.ai rate), but actual spend is $13.78/hour (79x discrepancy). The Kalman filter is using the OLD effective price, not the new compute_effective_price.

4. **Cron Script Contradictions** — The cron script reports "7 failures" for opencode_go but the direct test shows HTTP 200s in the last hour. The cron script is using stale data from before the flat router was enabled.

5. **Plan File Contradictions** — The plan file mentions "old path" code that should be removed, but we decided to keep it as a rollback safety net.

## Decisions to Make

### Decision 1: How to fix the PriceKalman initialization?

**Option A:** Initialize PriceKalman with $0.001/M for T4 providers (ollama_cloud, ollama_cloud_2)
- Pros: Correct immediately, no migration needed
- Cons: Requires code change, may break existing tests

**Option B:** Keep PriceKalman initialized with $0.40/M but add a migration to reset it
- Pros: No code change, backward compatible
- Cons: Requires manual intervention, not self-healing

**Recommendation:** Option A — initialize with the correct price. The PriceKalman will converge to the measured rate over time, but starting with the correct price makes it much faster.

### Decision 2: How to fix routstrd being chosen for glm-4.5-flash?

**Option A:** Remove routstrd from PROVIDER_MODELS for glm-4.5-flash
- Pros: Simple, immediate fix
- Cons: May break if ollama_cloud is exhausted

**Option B:** Keep routstrd in PROVIDER_MODELS but fix the effective cost for ollama_cloud
- Pros: More robust, handles ollama_cloud exhaustion
- Cons: Requires code change, more complex

**Recommendation:** Option B — fix the effective cost for ollama_cloud. This is the root cause of the problem. Once the effective cost is correct, routstrd will naturally be deprioritized.

### Decision 3: How to update the Kalman composition alert?

**Option A:** Update the cron script to use the new compute_effective_price
- Pros: Correct, no false positives
- Cons: Requires code change, may break existing tests

**Option B:** Keep the cron script using the old effective price but add a note about the discrepancy
- Pros: No code change, backward compatible
- Cons: False positives will continue

**Recommendation:** Option A — update the cron script. The false positives are causing alert fatigue.

## Implementation Plan

### Phase 1: Fix PriceKalman Initialization (CRITICAL)

**Goal:** Initialize PriceKalman with the correct effective price for T4 providers (ollama_cloud, ollama_cloud_2) and T3 providers (opencode_go).

**Changes:**
- [x] P1-1. Update zai_proxy.py: Change `_shadow_pk(0.40)` to `_shadow_pk(0.001)` for ollama_cloud, ollama_cloud_2, and opencode_go
- [x] P1-2. Add a comment explaining why we use $0.001/M for T3/T4 providers (marginal cost $0)
- [x] P1-3. Restart zai-proxy and verify the effective cost is now $0.001/M for ollama_cloud

**Verification:**
- [x] P1-4. Check the flat router's effective cost for ollama_cloud: `python3 -c "import sys; sys.path.insert(0, '/home/c03rad0r/.hermes/bot'); import flat_router; print(f'ollama_cloud effective cost: \${flat_router._get_effective_cost(\"ollama_cloud\", \"glm-4.5-flash\"):.6f}/M')"` — should show $0.001000/M, not $0.400000/M
- [x] P1-5. Check the flat router's candidate list for glm-4.5-flash: `python3 -c "import sys; sys.path.insert(0, '/home/c03rad0r/.hermes/bot'); import flat_router; candidates = flat_router.select_provider(model='glm-4.5-flash'); [print(f'{c.name:20s} \${c.effective_cost:.6f}/M') for c in candidates]"` — ollama_cloud should be first, not routstrd

### Phase 2: Fix Routstrd Being Chosen for glm-4.5-flash (CRITICAL)

**Goal:** Ensure routstrd is NOT chosen for glm-4.5-flash when ollama_cloud is available.

**Changes:**
- [x] P2-1. Update flat_router.py: Add a comment explaining why routstrd should be deprioritized for glm-4.5-flash (it's a paid provider and ollama_cloud can serve it for $0.001/M)
- [x] P2-2. Verify the flat router's candidate list: `python3 -c "import sys; sys.path.insert(0, '/home/c03rad0r/.hermes/bot'); import flat_router; candidates = flat_router.select_provider(model='glm-4.5-flash'); [print(f'{c.name:20s} \${c.effective_cost:.6f}/M') for c in candidates]"` — routstrd should be last, not first
- [x] P2-3. Restart zai-proxy and verify routstrd is NOT being chosen for glm-4.5-flash

**Verification:**
- [x] P2-4. Check the api_calls table: `sqlite3 /home/c03rad0r/.hermes/bot/zai_usage.db "SELECT key_name, COUNT(*) as calls, SUM(cost_usd) as spend FROM api_calls WHERE ts > $(date -d '1 hour ago' +%s) GROUP BY key_name ORDER BY spend DESC"` — routstrd should have 0 calls, not 475
- [x] P2-5. Check the routing_profit table: `sqlite3 /home/c03rad0r/.hermes/bot/zai_usage.db "SELECT provider_used, effective_price, COUNT(*) as calls FROM routing_profit WHERE ts > $(date -d '1 hour ago' +%s) GROUP BY provider_used ORDER BY calls DESC"` — ollama_cloud should be first, not routstrd

### Phase 3: Update Kalman Composition Alert (HIGH)

**Goal:** Update the cron script to use the new compute_effective_price, not the old effective price.

**Changes:**
- [x] P3-1. Update cost-escalation-check.py: Change the Kalman composition alert to use the new compute_effective_price
- [x] P3-2. Add a comment explaining why we use compute_effective_price, not the old effective price
- [x] P3-3. Restart the cron job and verify no false positives

**Verification:**
- [x] P3-4. Check the cron output: `cat /home/c03rad0r/.hermes/profiles/manager/cron/output/42a64624fb5d/$(ls -t /home/c03rad0r/.hermes/profiles/manager/cron/output/42a64624fb5d/ | head -1)` — should show no kalman composition alert, or the alert should be based on the new compute_effective_price

### Phase 4: Update Cron Script Contradictions (MEDIUM)

**Goal:** Update the cron script to use the new key_health table, not the old escalation_alert_state.json.

**Changes:**
- [x] P4-1. Update cost-escalation-check.py: Change the key health check to use the new key_health table
- [x] P4-2. Add a comment explaining why we use the new key_health table, not the old escalation_alert_state.json
- [x] P4-3. Restart the cron job and verify no false positives

**Verification:**
- [x] P4-4. Check the cron output: `cat /home/c03rad0r/.hermes/profiles/manager/cron/output/42a64624fb5d/$(ls -t /home/c03rad0r/.hermes/profiles/manager/cron/output/42a64624fb5d/ | head -1)` — should show no "7 failures" for opencode_go if it's actually working

### Phase 5: Update Plan File (LOW)

**Goal:** Update the plan file to reflect the decision to keep the old path as a rollback safety net.

**Changes:**
- [x] P5-1. Update plans/flat-router-cutover-2026-08-25.md: Add a note explaining why we keep the old path as a rollback safety net
- [x] P5-2. Update the checklist: mark the old path removal as "NOT DONE — kept as rollback safety net"

**Verification:**
- [x] P5-3. Check the plan file: `cat /home/c03rad0r/.hermes/bot/plans/flat-router-cutover-2026-08-25.md` — should show the correct status

## Testing

### Test 1: PriceKalman Initialization
- [x] T1-1. Initialize PriceKalman with $0.001/M for ollama_cloud
- [x] T1-2. Update PriceKalman with measured rate ($0.0155/M)
- [x] T1-3. Verify PriceKalman converges to $0.0155/M

### Test 2: Routstrd Deprioritization
- [x] T2-1. Select provider for glm-4.5-flash
- [x] T2-2. Verify routstrd is last, not first
- [x] T2-3. Verify ollama_cloud is first, not routstrd

### Test 3: Kalman Composition Alert
- [x] T3-1. Run the cron script with the new compute_effective_price
- [x] T3-2. Verify no false positives

### Test 4: Cron Script Contradictions
- [x] T4-1. Run the cron script with the new key_health table
- [x] T4-2. Verify no false positives

## Rollback Plan

If anything goes wrong:

1. **PriceKalman initialization:** `git checkout zai_proxy.py` — restores the old initialization
2. **Routstrd deprioritization:** `git checkout flat_router.py` — restores the old PROVIDER_MODELS
3. **Kalman composition alert:** `git checkout cost-escalation-check.py` — restores the old alert logic
4. **Cron script contradictions:** `git checkout cost-escalation-check.py` — restores the old key health check
5. **Plan file:** `git checkout plans/flat-router-cutover-2026-08-25.md` — restores the old plan file

## Next Steps

1. Review this plan and make any necessary adjustments
2. Implement Phase 1 (PriceKalman initialization)
3. Implement Phase 2 (Routstrd deprioritization)
4. Implement Phase 3 (Kalman composition alert)
5. Implement Phase 4 (Cron script contradictions)
6. Implement Phase 5 (Plan file)
7. Test everything
8. Commit and push

## Notes

- **Root cause bug found during implementation**: `_get_effective_cost` in flat_router.py used `model=model_id` on lines 639 and 647, but the parameter is named `model`, not `model_id`. This caused a `NameError` that was silently caught by `except Exception: pass`, making it fall through to the seed rate fallback. The seed rate fallback ALSO used `model_id`, so it failed too, and returned the raw seed rate ($0.40/M for T4 providers). Fix: changed `model_id` to `model` on both lines.

- The PriceKalman initialization fix ($0.40 → $0.001) is necessary but was NOT the root cause — the `model_id` NameError was. Even with $0.001 initialization, the `model_id` bug would have still caused the wrong effective cost.

- The routstrd deprioritization is automatic once the effective cost is correct — routstrd ($1.00/M) is naturally sorted after ollama_cloud ($0.001/M).

- The Kalman composition alert fix uses the flat router's `_get_effective_cost` instead of the stale `routing_profit` table (which stopped getting updates when the flat router was enabled).

- The cron script key_health fix adds a `recent_ok` check: if a key has had successful API calls in the last hour, it's working — don't alert on stale failure counts from before the flat router was enabled.