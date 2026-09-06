# Fleet Recovery + Ollama Key #4 + Chutes Wiring — 2026-09-06

**Date:** Sun 2026-09-06 (~06:10 IST, updated ~06:35)
**Status:** IN PROGRESS
**Trigger:** The manager session went deaf again ("model provider failed after
retries" storm, ~05:50–06:07). Live diagnosis (read-only, verified):

- `ours` (z.ai sub key) serves **glm-5.2 with 200-but-EMPTY completions** —
  112 empty / 136 calls in 35 min. glm-5.3 on the same key: 99 good / 3 empty.
- `key_health` marks the whole failover chain dead: `ours` exhausted (304
  fails), `ollama_cloud/2` "exhausted" (59 fails — direct probes confirmed
  LIVE 429s at ~06:14, so the marks were TRUE, not stale), friend/routstr/
  routstrd/ppq/openrouter/deepinfra `dispatch_fail` (402s).
- `/quota` claims all three ollama pools full/included while the keys 429 —
  a quota-view vs key-state discrepancy (source TBD, board task).
- Bleed: manager traffic descends into routstrd ($0.53/M, 30 calls/hour).
- NW: kWh period topped up (86 kWh total, ~8.9 left at 89.6%), daily cap fine
  ($1.03/$10). NW + OR carrying the post-restart relief load.

## Phase 0 — Add ollama key #4 (sleepy_easley_477 → ollama_cloud_4)

Felix bought the 4th key (reversal of the 2026-09-02 cancellation — the
provider-hunt pipeline since failed to deliver: OI contact-gated, Chutes +
Baidu canaries failed, batch = $0 savings). ~+500M tok/wk at the fleet's
measured cheapest rate.

- [ ] 0.1. Direct probe the new key (3 tokens, $0) — confirm the 500M weekly
      pool is live; certificate/plan shape matches $20/mo baseline
- [ ] 0.2. Same-window probe keys 1–3 — settle the /quota-vs-429 discrepancy
      (did the old pools recover post-06:14?)
- [ ] 0.3. Store the key (no echo): `OLLAMA_CLOUD_API_KEY_4_SLEEPY_EASLEY_477`
      → `~/.hermes/profiles/manager/.env` (600; account-suffix naming per
      the key-3 convention)
- [ ] 0.4. `zai_proxy.py`: .env-parser elif; `OLLAMA_CLOUD_KEY_4` extraction
      + key tuple; `_KEY_COST_MULTIPLIER`; exhausted-marks/quota plumbing;
      (weekly-pool key like #1/#2 — no monthly special-casing)
- [ ] 0.5. MRE `flat_router.py`: provider entry, price multiplier,
      billing-regime, routing-priority list, key-set membership checks
- [ ] 0.6. MRE + bot-mirror `real_price_tracker.py`: `ollama_cloud_4` seed
      $0.0155/M + per-key measurement entry (MRE = source of truth)
- [ ] 0.7. `config/providers.yaml` ollama stanza (if per-key entries)
- [ ] 0.8. Extend tests: env-parser + `_ollama_cloud_key_order` four-key case
- [ ] 0.9. Full bot suite green (baseline: 12 pre-existing failures)
- [ ] 0.10. Proxy restart in a quiet gap (watch the manager's ADR-work
       stream for a lull)
- [ ] 0.11. Verify: `/quota` shows 4 pools; smoke request lands on key #4
       (fullest pool sorts first per remaining-quota order); `key_health`
       row healthy; fresh api_calls 200s with content
- [ ] 0.12. If keys 1–3 probes passed: also clear their health marks in the
       same restart (restores ~1.5B/wk flat capacity) — else leave marks
       (they're honest) and record the /quota discrepancy on the board
- [ ] 0.13. Commits: bot (zai_proxy + tests + bot/src mirror + this doc)
       → push `dr`; MRE (flat_router + tracker + providers.yaml) → its
       remote; plan-doc verification log

## Phase 1 — Un-deafen the manager

- [x] P1a. Pre-restart check done (traffic fully stalled at 06:11 — nothing
      in flight to lose)
- [x] P1b. Proxy restarted 06:11:59; active + `/health` ok
- [x] P1c. ollama keys re-probed directly (06:14): **all three returned
      live 429s — marks TRUE, not stale**. The /quota-vs-429 view mismatch
      remains open → board task (P4a) + resolved operationally by Phase 0
      adding fresh capacity
- [x] P1d. Empty-storm ended: post-restart traffic (openrouter 21×,
      NW 30×) shows **zero empty completions**; no glm-5.2-on-ours rows since
- [x] P1e. Model flips EXECUTED (not just contingency): root config default
      + `delegation.model` glm-5.2 → glm-5.3; gateway restarted 06:16:11.
      Contingency fallback never needed — glm-5.3 verified good on ours

## Phase 2 — Stop the bleed

- [x] P2. routstrd bleed stopped: zero routstrd rows since the restart
      (last 30-min windows show openrouter + NW only)

## Phase 3 — Wire the new Chutes lane (per ADR-014 discipline)

- [x] P3a. Chutes key extracted programmatically from the manager message
      store → `.env` (no echo; old cpk_21df… replaced by cpk_79a2…)
- [x] P3b. "Registration" resolved as: registry entry already existed
      (`chutes` in MRE providers.yaml, TAO prices, status
      `go_conditional_pending_account`); the account Felix bought satisfies
      the pending condition. Lane CODE does not exist in flat_router —
      gated behind the canary per ADR-014, so no routing code until pass
- [x] P3c. **Canary run ×2 — BOTH FAILED, gate held, nothing routes:**
      - `deepseek-ai/DeepSeek-V4-Flash-0731-TEE`: 15/20 (75%, bar 90%) —
        4× repetition-blowup + 1× HTTP 502
      - `zai-org/GLM-5.2-TEE`: worse — repetition blowups + empty responses
      Reports: `~/merchant-routing-engine/reports/canary/`. Diagnosis
      deferred (defaulted to the manager session's ADR-014 owner of the
      follow-up; possibly temperature/model-variant retry)
- [ ] P3d. OpenInference: signup is CONTACT-GATED (no self-serve route —
      the site is a JS shell with only /blog /contact /privacy /terms;
      Google's "OpenInference" is a different thing — Arize's telemetry
      SDK). Action: email `markian@openinference.ai` (draft for Felix to
      send). API verified live (`api.openinference.ai/v1`, 2 DeepSeek-V4
      models, 1M ctx). Projected $0.0093/M eff, $50 deposit ceiling

## Phase 4 — Systemic follow-ups

- [ ] P4a. Board task on `router-maintenance`: the **/quota-vs-key-state
      discrepancy** + stale-health recovery — /quota says pools full while
      keys 429; exhausted keys need a re-probe loop against live pool state
      (weekly reset must not require a proxy restart to recover)
- [ ] P4b. NW top-up reminder when the kWh period flips (user action;
      8.9 kWh left at current pace ≈ short runway)
- [ ] P4c. Key hygiene: OR + Chutes + ollama#4 keys exist in chat
      transcripts — rotate through dashboards after wiring is verified
- [ ] P4d. This doc's verification log + commits + push

## Cheap-token roadmap (from the 2026-09-06 consultation; pending Felix GOs)

- T1 ✓ BOUGHT: ollama key #4 (this Phase 0)
- T2a OpenInference email draft (P3d above)
- T2b Baidu-0731-via-OR canary re-run + root-cause the 70% fail (~$0.50,
  on the funded $8.25 key) — manager session's ADR-014 flow
- T2c Chutes TEE-quality diagnosis (P3c follow-up)
- T3 Demand-side (board QS tasks): D4 cache-hit accounting → QS-9 cache-aware
  dispatch + QS-10 prefix hygiene + QS-11 reasoning_effort=low
- T4 provider-hunt cron keeps scouting (already automated)
- Ruled out (verified verdicts): batch-mode rewiring ($0 savings; OR :batch
  trap), GPU-rental market, more Chutes money pre-diagnosis

## Out of scope (owned by the manager session)

- ADR-014 remaining flow: Baidu canary FAIL root-cause, OpenInference onboarding
- The `glm-5.2` z.ai response-shape diagnosis once lanes are healthy

## Verification log

- 06:11 Proxy restarted (traffic was stalled; zero loss). Active + healthy.
- 06:14 Direct probes: ollama keys 1–3 → live 429s. Marks TRUE. Chatutes
  key stored, catalog pulled (14 TEE models).
- 06:16 Gateway restarted after glm-5.2→glm-5.3 flips (root default +
  delegation). Manager's work stream resumed (OR deepseek traffic 06:20+).
- 06:29/06:32 Traffic: openrouter 21× + NW 30× (glm-5.2 relief + deepseek),
  zero empties, zero routstrd. NW: 89.6% used, 8.9 kWh left, $1.03/$10 day.
- 06:49 Chutes canaries ×2 FAILED (75% / ~65% vs 90% bar) — gate held.
