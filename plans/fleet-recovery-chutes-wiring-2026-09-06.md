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

- [x] 0.1. Direct probe the new key (3 tokens, $0) — confirm the 500M weekly
      pool is live; certificate/plan shape matches $20/mo baseline
- [x] 0.2. Same-window probe keys 1–3 — settle the /quota-vs-429 discrepancy
      (did the old pools recover post-06:14?)
- [x] 0.3. Store the key (no echo): `OLLAMA_CLOUD_API_KEY_4_SLEEPY_EASLEY_477`
      → `~/.hermes/profiles/manager/.env` (600; account-suffix naming per
      the key-3 convention)
- [x] 0.4. `zai_proxy.py`: .env-parser elif; `OLLAMA_CLOUD_KEY_4` extraction
      + key tuple; `_KEY_COST_MULTIPLIER`; exhausted-marks/quota plumbing;
      (weekly-pool key like #1/#2 — no monthly special-casing)
- [x] 0.5. MRE `flat_router.py`: provider entry, price multiplier,
      billing-regime, routing-priority list, key-set membership checks
- [x] 0.6. MRE + bot-mirror `real_price_tracker.py`: `ollama_cloud_4` seed
      $0.0155/M + per-key measurement entry (MRE = source of truth)
- [x] 0.7. `config/providers.yaml` ollama stanza (if per-key entries)
- [x] 0.8. Extend tests: env-parser + `_ollama_cloud_key_order` four-key case
- [x] 0.9. Full bot suite green (baseline: 12 pre-existing failures)
- [x] 0.10. Proxy restart in a quiet gap (watch the manager's ADR-work
       stream for a lull)
- [x] 0.11. Verify: `/quota` shows 4 pools; smoke request lands on key #4
       (fullest pool sorts first per remaining-quota order); `key_health`
       row healthy; fresh api_calls 200s with content
- [x] 0.12. If keys 1–3 probes passed: also clear their health marks in the
       same restart (restores ~1.5B/wk flat capacity) — else leave marks
       (they're honest) and record the /quota discrepancy on the board
- [x] 0.13. Commits: bot (zai_proxy + tests + bot/src mirror + this doc)
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
- [x] P3d. OpenInference: signup is CONTACT-GATED (no self-serve route —
      the site is a JS shell with only /blog /contact /privacy /terms;
      Google's "OpenInference" is a different thing — Arize's telemetry
      SDK). Action: email `markian@openinference.ai` (draft for Felix to
      send). API verified live (`api.openinference.ai/v1`, 2 DeepSeek-V4
      models, 1M ctx). Projected $0.0093/M eff, $50 deposit ceiling

## Phase 4 — Systemic follow-ups

- [x] P4a. Board task on `router-maintenance`: the **/quota-vs-key-state
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

## Verification log (Phase 0 execution, 06:4x–07:0x)

- 06:41 Key probes: key #4 → **HTTP 200 (live, 500M pool)**; keys 1–3 →
      live 429s (marks honest; /quota-vs-reality mismatch documented).
- 06:44 Key stored (`OLLAMA_CLOUD_API_KEY_4_SLEEPY_EASLEY_477`, no echo).
- 06:45-06:56 Wiring: zai_proxy (parser, key set, paywall flags, quota/
      health/fallback/shadow maps, ×4 dispatch gates, snapshot payload),
      MRE flat_router + real_price_tracker (both copies), providers.yaml
      note. 14 code touchpoints total.
- 06:58 Tests: 11 pool/cap tests + oc3 fixtures updated for 4-key order;
      full suite **505 pass / 12 baseline failures** (identical set).
- 06:49 Quiet-gap proxy restart (traffic ≤1 call/45s); active + healthy.
- 07:0x Post-restart: `/quota` shows **4 pools**; keyHealth oc4 row
      healthy=1/0 fails; **3/3 deepseek smokes served by ollama_cloud_4
      (200, deepseek-v4-flash:0731)** — fresh pool enters rotation first
      exactly as designed; glm-5.3 stays on the free z.ai lane; zero
      routstrd rows (Phase 2 holds).
- 07:1x Commits: bot `831587d` on dr main (worker-branch cherry-pick +
      autostash rebase over the concurrent session's d0657de); MRE
      `363a575` (lane + chutes canary evidence). Board task `t_30dde4c7`
      created (quota-view mismatch). OI email draft in MRE
      docs/provider-hunt/OPENINFERENCE-ONBOARDING-EMAIL-2026-09-06.md.

Remaining open items: P4b (NW top-up watch), P4c (key rotations —
OR/Chutes/ollama#4 live in transcripts), T2b Baidu canary re-run,
T2c Chutes TEE diagnosis, T3 demand-side QS tasks (D4 cache-hit
accounting first).

## Round 2 — "hermes unresponsive" postmortem (2026-09-06 ~11:00)

User-visible: manager DM deaf again since ~07:26 (Felix's paste = the
manager's own Chutes-lane session, timestamps in his local TZ).

Causal chain (all IST):
- 07:05-07:26 z.ai empty-stream disease hit glm-5.3 too (24/25 empties
  at 07:06; glm-5.2 had it at 06:05). Transient — both planes healthy
  by 10:44 (probes clean).
- ours tripped its WEEKLY PACING LOCK (74% vs 60% thr) — correct
  governor behavior, but it left glm with zero zai keys.
- friend lane = KEYLESS (ZAI_API_KEY absent from manager/.env — open
  question in t_69da44d9; GLM_API_KEY/GLM_BASE_URL in .env may be it).
- routstr-forward-tunnel held by a stale Sep-03 ssh (PID 3018) → unit
  crash-looped on bind → routstr/routstrd broken-pipe spin. FIXED:
  killed stale ssh, fresh unit line up, node answers (11:06).
- ppq/deepinfra chronically dispatch-failing (independent outage).
- Result: glm-5.3 walk = [routstr,routstrd,deepinfra] all dead → 503
  ×54, gateway retry-death ×37, DM silent. Chutes (wired by the
  manager at 07:17) kept serving buyer traffic cleanly throughout.

Recovery executed:
- Root config + manager + treasurer default model → deepseek/deepseek-v4-flash
  (profile configs override the root config — only root flip was NOT
  enough before the 11:00 restart; worker-reviewer-glm/kimi-consultant
  stay model-specific by design, paused until zai recovers).
- Stuck 146-msg DM conversation (20260906_050400_7c3f4068, ~126K ctx,
  dropped tool args) exported + deleted: backups/session-exports/
  20260906_105800_7c3f4068-pre-reset.jsonl. Fresh sessions = deepseek.
- Gateway restarted twice (config reload). Manager one-shot verified
  clean ("unblocked-ok" via chutes). First traffic: chutes/DS 200s.
- Manager's uncommitted Chutes work reviewed + tested + committed:
  3c08486 (chutes lane + t_30dde4c7 server-truth quotas + stale-backoff
  recovery + recovery tests). glm-5.2 then pulled OFF chutes (ce47af0):
  mid-stream failure with no failover — client hangs (t_bc670026).
- Marks cleared (ours/friend/routstr/routstrd/deepinfra/ppq), ollama
  key #4 + chutes remain healthy carriers for deepseek.
- Board: t_bc670026 (streaming failover hole), t_69da44d9 (zai-tier
  false-negative + friend key mystery), t_303cc82e (test isolation).

Suite: 15 failed vs 12 baseline — delta = ambient-state test flakiness
(ours' live lock leaks into tests via zai_proxy_state.json; verified by
stashed-HEAD runs). NOT regressions. Hermetic fix = t_303cc82e.

Still degraded (by design, with owners):
- glm-5.2/glm-5.3 via proxy until ours' weekly window recovers OR the
  friend key is restored (t_69da44d9). root-config escape hatch
  (direct z.ai fallback) unaffected.
- friends/public resale chain regression (glm not resolving on public
  after the manager's 03:4x-era DB surgery) — manager's own task,
  picked up post-recovery via its plan.
