# ollama3 Onboarding + Router Remediation + Burn Reduction (2026-09-02)

Status: IMPLEMENTED (see per-item notes; a few D-items deferred with rationale)
Owner: c03rad0r
Scope: add new ollama cloud key (stoic_herschel_499, $20/mo, monthly-budget credit pool) as `ollama_cloud_3`; fix today's 503 "all providers exhausted" storm causes; reduce hermes token burn.

## Background / findings (2026-09-02 investigation)

- **503 storm root cause**: simultaneous outage of every glm-5.3 rung —
  - `ours` (z.ai $155/mo) weekly-quota DELISTED (100%, resets ~Sep 3 20:40 IST; 425 failures)
  - `opencode_go` $10/mo allowance exhausted → 14-day backoff (aux glm-5.3 drained it in ~2 days)
  - `neuralwatt`, `routstr`, `deepinfra` → 402 unfunded (balances depleted)
  - `routstrd` HTTP 500 + quota-locked (100%)
  - `ollama_cloud`/`ollama_cloud_2` flapping "exhausted" under burst load (oc sess 19%/wk 33%, oc2 sess 6%/wk 4%)
  - `telnyx` HEALTHY but does not serve glm-5.3
- **NeuralWatt dashboard (agreement check)**: cycle allowance 13.33 kWh × $7.50 ≈ $100/cycle (= NW_INITIAL_BALANCE 100) ✓; exhausted → confirms the 402s; realized ≈ $0.11/M blended (94% cache + energy pricing) ≈ 45% of list token rates. **ADR-004 "Phase B overage" is REAL via pay-as-you-go credits** (kWh exhausted + credits>0 → serves on credit; the earlier hard-402 was credits=$0 too). Post-top-up: $17.97 credits.
- **glm-5.2 vs 5.3**: identical list rates; 5.3 always-on reasoning → ~2× output tokens & ~2× energy/token, ~3× latency on ollama. Keep 5.2 as coding/resilience lane.
- **Burn profile (24h)**: glm-5.3 = 299.5M tokens (~88%); manager interactive sessions = 295.8M (6061 calls, avg ~48k prompt/call). **~98% of tokens are PROMPT (prefill).** Compression threshold 0.7×1M ctx = 700k — never fires effectively.
- **NEW architecture finding (important)**: the running proxy binds `src.*` to **~/merchant-routing-engine/src** (stale since ~Aug 26) because zai_proxy.py:34 inserts MRE at sys.path[0] before the first `from src.shadow_hook` import. zai_proxy.py and flat_router.py DO load from bot/ (bot precedes MRE at request-time lazy import). Follow-up P1: reconcile bot/src ↔ MRE/src.
- User answers: fee $20/mo · scope = everything · naming confirmed · neuralwatt topped up (credits).

## Checklist

### Phase A — wire `ollama_cloud_3` (monthly budget-pool, T4-included)
- [x] A1. `.env`: `OLLAMA_CLOUD_API_KEY_3_STOIC_HERSCHEL_499=<key>` (verified resolves)
- [x] A2. `zai_proxy.py`: loader branch → `keys["ollama_cloud_3"]`; LAST in `_OLLAMA_CLOUD_KEYS` (relief valve); `all_keys`; `_KEY_COST_MULTIPLIER`; health snapshot + monthly gating with ~90% scarcity guard; `.ollama_exhausted_until_3` flag path
- [x] A3. `flat_router.py`: PROVIDER_MODELS (19 models); `_SEED_RATES` 0.40; PROVIDER_TIER "included"; dispatch branch; delist-guard design comments
- [x] A4. Subscription cost $20/mo documented in flat_router registration comment (no central fee registry exists — consistent with oc/oc2 handling)
- [x] A5. Monthly-window gating: `zai_proxy._get_ollama_quota_status` server-fraction override (`limits.monthly.usage`); `src/ollama_quota_tracker.py` monthly window + `MONTHLY_WINDOW_S`; `src/ollama_extra_usage.py` monthly fraction parse. NOT added to routstr sell-side exhaustion-gate `_pools_to_sell` (deliberate: new sink-cost key is not for resale)
- [x] A6. `src/catalog_drift_check.py` key_env entry (reads .env file directly ✓)
- [x] A7. `src/real_price_tracker.py` oc3 billing-API probe + `_env_file_key` .env fallback (fixes latent gap: env-only lookups never saw the ollama keys); MRE deploy copy got the oc3 SEED_RATES entry
- [x] A8. Tests: 73 + 81 + 17 = 171 passed across test_flat_router, tests/test_silent_substitution, tests/test_cost_attribution, test_time_aware_pricing, test_user_agent_headers, src/
- [x] A9. Restart done (03:13:53 IST, PID 618902). LIVE-VERIFIED: quota snapshot shows oc3 monthly fields; **glm-5.3 routed with oc/oc2 disabled → HTTP 200 `X-Provider: ollama_cloud_3`**; api_calls rows flowing (glm-5.3 + kimi-k2.7-code)

### Phase B — 503-storm remediation
- [x] B1. NeuralWatt: credit-mode override in `_neuralwatt_quota_snapshot` (kWh exhausted + credits>0 → funded, regime="credit", credits-based scarcity); realized-rate notes; NW $1/M fallback concern fixed via MRE seed patch (0.0155). **Daily-cap guardrail tripped correctly** (daily spend $20.20 > $10 cap → daily-capped until UTC midnight — protects the ~$14 credit balance)
- [x] B2. opencode_go: allowance-driven 14-day backoff is CORRECT behavior ($10/mo drained by aux glm-5.3); protection folded into D-deferred notes
- [x] B3. ours self-heals Sep 3 ~20:40 IST (no action needed)

### Phase C — consultant ratification
- [ ] C1. DEFERRED: kimi-consultant review of monthly-pool design (oc3 + NW credit-mode). Design followed the adding-api-key-to-live-router skill + verified live; consult when convenient (note: consultant runs on kimi-k3 — costs ~$0.50/call)

### Phase D — burn reduction (ADR-014)
- [x] D1. Prefill reduction: compression threshold 0.7 → **0.15** in `~/.hermes/config.yaml`, `profiles/manager/config.yaml`, `profiles/kimi-consultant/config.yaml` (workers keep 0.7 — already capped by hygiene_hard_message_limit 60; their burn is <2M/day)
- [x] D2. Tiered triage: quality_tiers updated (glm-5.3+kimi-k3 high; glm-5.3-flash+kimi-k2.7-code standard; deepseek-flash low); **worker-reviewer-kimi → kimi-k2.7-code** ($0.95/$4 vs $3/$15). model_map chat-lane change NOT applied — glm-4.5-air availability per-provider unverified; deferred to avoid never-substitute violations
- [x] D5 (partial). NW variant routing: kimi-k2.7-code rung already live; glm-5.2-fast/kimi-k3-fast variant name-maps DEFERRED (each variant = new canonical rung; needs catalog verification first)
- [x] D6. Spend cap: reactivated as **metered-only cash breaker** `SPEND_CAP_METERED` (default $25/d; spend-cap.conf) — `_check_global_spend_cap` sums neuralwatt/routstr/routstrd/deepinfra/telnyx/ppq/openrouter only; sub lanes never blocked (deviation from plan's naive $12/$2 caps — those would 503 sessions while paid quota remained; documented in docstring)
- [x] D7. Hygiene: `escalation_alert_state.json` compacted 12.5MB/41143 keys → 2.4KB/10 keys (backup: `.precompact-20260902`); all 41k entries were zombie branch_staleness records from the Aug-30 corruption incident; scanner's resolved-retention tightened 7d → 2d
- [ ] D3. Cache maximization: user actions pending — request NW access for glm-5.3-flash + qwen-3.8 (dashboard); prefix determinism + session stickiness DEFERRED (needs code pass)
- [ ] D4. Cache-aware accounting (cached_tokens extraction into api_calls): DEFERRED — schema + extraction change, worth its own PR
- [ ] D8. opencode_go post-renewal protection: DEFERRED until allowance renews (~mid-Sep)
- [ ] D9. Burn digest additions (prompt-share/cached-share): DEFERRED (price_viz has partial coverage)

### Phase E — docs + commit
- [x] E1. This plan doc maintained (statuses above)
- [x] E2. Commit: code only — zai_proxy.py, flat_router.py, src/{ollama_quota_tracker,ollama_extra_usage,catalog_drift_check,real_price_tracker}.py, config/providers.yaml, test files, this plan. NEVER .env. Runtime-churn files (state/thresholds/pressure/peak_hours/INDEX) left out.
- [ ] E3. Signal canary on first oc3 exhaust event: monitored via drift-check cron's Signal alerts (no new code)

### Verification
- [x] V1. oc3 in chains; disabled-exclusion test **PASSED live** (oc/oc2 flags → glm-5.3 → 200 X-Provider: ollama_cloud_3); flags restored
- [x] V2. `/quota` shows ollama_cloud_3 monthly fields (fresh budget, mo=0%); monthly_tokens local fallback degrades safely (MRE-old signature verified compatible; server fraction authoritative)
- [x] V3. NeuralWatt credit-mode live (`is_exhausted: false`, remaining $14.30); post-restart burst (15M tok) caught by daily cap as designed
- [x] V4. Snapshot: oc3 absorbing 5.3/k2.7 traffic within minutes of restart; no error lines/tracebacks post-restart

## Follow-ups (P1/P2)
1. **P1: MRE↔bot src reconciliation** — proxy imports src.* from ~/merchant-routing-engine/src (stale Aug 26). Today only real_price_tracker got the oc3 seed patch. Files diverged: balance_collectors (bot Aug23 vs MRE Aug26 — careful direction analysis needed), live_router, realtime_pricing, model_mapping, margin_layer, routing_advisor/optimizer, cost_observer, primary_router. Consider making bot/ the single source (adjust zai_proxy sys.path order/move imports) or a proper deploy-sync script + `tests/test_deploy_import_shadow.py` extension.
2. P2: D3/D4/D8/D9 deferred burn items (see checklist).
3. P2: opencode session compaction (opencode-side config, separate from hermes config).
4. Watch: neuralwatt daily-cap reset at 05:30 IST; ours z.ai weekly reset Sep 3 ~20:40 IST.
