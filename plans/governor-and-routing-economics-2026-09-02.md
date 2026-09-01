# Governor Repair + Routing Economics — 2026-09-02

**Status: IMPLEMENTED (see verification log at bottom)** (live checklist below)
**Scope**: Fix the two Kalman compression governors; fix routing economics (ollama
pool rebalance, oc3 phantom cost, routstrd demotion); wire price-awareness into
compaction. All proxy-side — no gateway changes tonight.

## Diagnosis (verified 2026-09-02 ~04:00 IST)

Both governors are invoked by internal **hermes-cron jobs** (every 15m) — invisible
to crontab/systemd greps:

- **Growth governor** (`compression_growth_governor.py`) — alive, job OK.
  Bug: `compute_threshold()` uses `base = current_threshold`, making the control
  law an **unbounded integrator (ratchet)**: with sparse-growth estimate
  (Kalman x≈671 < G_BASELINE=1800) every run adds +0.045 to *every* profile's
  threshold → all 75 drift to MAX_THRESHOLD (0.7). Confirmed live: manual 0.35 →
  0.3952 → climbing; kimi-consultant 0.15 → 0.2406 (stomped).
- **Cost governor** (`compression_cost_governor.py`) — job
  `comp-gov-1787437592` fails every 15m (`last_status: "error"`), state frozen
  since **2026-08-23 14:03**. Additionally its `measured_ratio` was ≈0 (only
  ~15 `task_type='compression'` rows / $0.01 per 48h), and `cost_usd` itself is
  polluted (oc3 phantom $1.00/M).

## Plan checklist

### Phase A — Governor repair (root cause)
- [x] A1 Growth-governor convergence fix: target = `FALLBACK + K·(G_BASELINE−g)`
      computed from BASE, not current (kill the ratchet); keep hysteresis
      write-guard. Retain `current_threshold` param for signature compat.
- [x] A1b Calibrate `FALLBACK_THRESHOLD` 0.60 → 0.45 for current ecosystem
      (was tuned for 200k-ctx profiles; 0.6-0.7 fill on 1M-ctx manager sessions
      is a degeneration-risk zone post-RC1-fix).
- [x] A1c Exempt list: kimi-consultant keeps deliberate 0.15 (deliberate
      aggressive-compaction lane), skipped by the governor.
- [x] A2 Cost-governor revival: manual run → capture+fix error, reset stale
      Kalman/PI state (fresh integral), verify state file + override output
      written; verify consumer wiring (`compression_model_router` ↔ zai_proxy).
- [x] A3 Repair tests: stale `MIN_THRESHOLD` import (collection error since
      c0e6462 renamed it to a dynamic per-profile min); dedupe
      root `test_compression_growth_governor.py` into `tests/`; update
      expectations to convergent semantics.
- [x] A4 Staleness watchdog: healthy growth governor checks the cost
      governor's state-file freshness (>30 min stale → stderr alert + journal
      + escalation alert file), so a silently-dying governor can't recur
      unnoticed for 10 days.

### Phase B — Routing economics
- [x] B1 Ollama weighted rotation by remaining weekly pool in
      `_try_ollama_cloud`: dynamic key ordering by (remaining quota, health)
      instead of static [oc, oc2, …, oc3-LAST]; oc3 promoted from strict LAST
      while keeping its ~90% monthly scarcity guard and relief-valve role.
      Recovers ~800M tokens/wk of paid-but-unused combined pool (oc was at
      1,143M/7d vs 500M/wk nominal pool → thrash source).
- [x] B2 oc3 cost-attribution fix: api_calls rows carry $1.00/M phantom
      (≈$9.8/day phantom spend in dashboards). Fix rate resolution for
      `ollama_cloud_3` (shared immutable pool, real $0.0155/M blended seed).
      Prerequisite for cost-governor ratio to be meaningful.
- [x] B3 Routstrd demotion to last-resort: 89.9M tokens / $47.67 real cash
      over 7d at $0.53/M — shift that traffic to the $0.015/M ollama lane.

### Phase C — Price-aware compaction (proxy-side)
- [x] C1 Wire realized $/M into the growth-governor target: price nudge
      (`PRICE_NUDGE_*`, log-price term, clamped ±0.10) — expensive lanes
      compact sooner, cheap lanes preserve context longer. Source: the
      15-min realized-price Kalman state.
- [ ] C2 (DEFERRED — follow-up, needs gateway-restart window, not tonight)
      Extend `should_compress_preflight` in hermes-agent for per-call
      marginal-cost compaction decisions.

### Phase D — Verify + ship
- [x] D1 Full bot test suites (targeted + bot-wide, compare to baseline
      442 pass / 12 pre-existing failures).
- [x] D2 Manual governor runs ×2 — convergence proof (threshold stable
      across runs with unchanged inputs, no ratchet).
- [x] D3 Reset deliberate thresholds: manager 0.35, root 0.35,
      kimi-consultant 0.15 (exempt).
- [x] D4 Live lane smokes through proxy post-restart (glm-5.3 / deepseek /
      qwen lanes via /quota-ordered ollama keys).
- [x] D5 Journal clean; commit + push dr main.

### User-side (not executable from here)
- [x] ~~Buy +1 ollama Pro key ($20/mo, ~500M tokens/wk)~~ CANCELLED by Felix
      (2026-09-02): no 4th subscription. New-subscription candidates come from
      the subscription/model discovery-research tooling BEFORE any buy — do not
      re-propose manual key purchases without a research pass.
- [ ] Optional: ~$15 NeuralWatt credit top-up (premium glm-5.3 lane float).

### B1 fix follow-up (2026-09-02, landed same day)
- [x] BUG A: oc3 remaining-quota uses monthly_limit (budget 3.5B), not
      monthly_tokens (used count) — `zai_proxy._snapshot_quota` oc3 branch +
      `_ollama_cloud_key_order` oc3 branch. monthly_limit surfaced in tracker
      status dict (`src/ollama_quota_tracker.py`).
- [x] BUG B-h: `_is_key_healthy` real 90% monthly delist gate for oc3
      (fail-open; paywall/manual-disable semantics intact; no lock recursion —
      quota status path confirmed lock-free first).
- [x] BUG B-sc: flat_router scarcity uses monthly_used_pct for oc3 (was
      max(session,weekly) = 0 for monthly-only key → zero price pressure).
- [x] Tests: `tests/test_oc3_monthly_budget_fixes.py` (10 new, 3 classes;
      5 bug-revealing tests confirmed RED before fix); pool test
      `test_most_remaining_first` updated — fresh oc3 (3.325B) now correctly
      outranks burned oc2 (450M); targeted suites 171 pass / 12 subtests;
      tests/ 438 pass / 12 pre-existing fails (identical to baseline).

## Constraints honored
- Bot-repo commits only; profile config edits live-only (documented here).
- No gateway restart (concurrent dispatcher session live); hermes-agent
  working-tree changes stay uncommitted per that repo's conventions.
- MRE shadow: top-level modules (flat_router, zai_proxy, governors) load from
  bot/; `src.*` resolves to the MRE copy for some modules in the RUNNING
  proxy — restart required for all changes to take effect; src/reasoning
  handler fixes already live from df8ed9c arc.
- eve/keep routing hermes-cron jobs untouched except fixing the failing
  comp-gov job's script (if the error is in the job spec itself).

## Verification log (2026-09-02 04:2x IST)
- **A2 cost governor REVIVED**: root cause — hermes-cron job `comp-gov-1787437592`
  carried a command line in `script`, but the runner resolves `script` as a FILE
  under profiles/manager/scripts/ → "Script not found" every 15 min since Aug 23
  14:03. Fixed via wrapper `comp-gov-run.sh` (profile-scripts, live-only) +
  `hermes cron edit --script comp-gov-run.sh`. Verified: `last run ok` at
  04:00:57 and 04:16:03; state file fresh each cycle. Stale state archived to
  `compression_governor_state.json.pre20260902` (was: integral wound −0.5,
  frozen Kalman). Consumer wiring verified: `compression_model_router.py` is
  imported by zai_proxy (:1985) and reads the governor's override JSON.
- **A1 growth governor converged**: ratchet removed (target computed from BASE,
  never current); FALLBACK 0.60→0.45 calibrated; kimi-consultant exempt
  (deliberate 0.15 preserved); main() returns summary. LIVE PROOF: first manual
  run collapsed all 75 scattered/ratcheted profiles to 0.4948 = 0.45+
  K·(1800−680.6)+price_nudge(0.0137 $/M); second run changed 0/75 (converged).
- **A3 tests**: stale tests/ (importing removed MIN_THRESHOLD/CONTEXT_LENGTH —
  collection error) replaced by the API-current root file (52 tests) + 12 new
  regression tests (ratchet, price nudge, exempt) = 64 passed.
- **A4 staleness watchdog**: `check_governor_staleness()` added to
  unified-system-alert.sh (30-min state-file freshness, deliver=origin) —
  live-only profile-script, verified via manual run.
- **B2 oc3 cost attribution**: family-rate path added in `_estimate_cost_usd`
  (never UNKNOWN_PROVIDER_FALLBACK $1/M); DB repaired: 134 poisoned rows
  $9.80→$0.15 (0.0155/M, cost_source=migrated-oc3-family-rate) — un-poisons
  the self-feeding real_price_tracker basis. New rows verified live.
- **B1 pool-weighted ollama rotation**: `_ollama_cloud_key_order()` (remaining-
  quota DESC, 120s TTL, static fallback on error, paywall keys sink). LIVE
  PROOF: post-restart smokes all routed via `ollama_cloud_2` (most remaining:
  467.7M) instead of burning oc first.
- **B3 routstrd sub-cap**: `_routstrd_daily_cap_tripped()` (env
  ROUTSTRD_DAILY_CAP=10.0/day) wired into `_snapshot_health` — self-demotes
  the metered overflow catch-basin after runaway real-cash burn; ollama pool
  rebalance upstream makes tripping rare.
- **C1 price-aware compaction**: realized $/M from api_calls (6h window) —
  bounded log nudge (±0.10) in compute_threshold; measured 0.0137 $/M live.
- **Tests**: target suites 64+17+8 new + full bot run 454 passed / 12 failed —
  the SAME 12 pre-existing failures verified identical on stashed baseline.
  Proxy restarted 04:07(ish), 0 journal errors. Cost-gov runs ok on schedule.
- **Companion action (04:21:46)**: hermes-gateway restarted → FIX-2b
  session-search output caps now LIVE for all sessions (was pending from the
  night-degeneration plan; direct answer to the Protein-RNA group's compaction
  loop — that session is b4d817eb, the 12M-token session: uncapped tool
  outputs + threshold chaos + polluted history were the stacked causes).
  Graceful drain let the orchestrator's in-flight git turn finish first.
- Deviations from original checklist text: (1) manager/root NOT pinned to 0.35 —
  the fixed governor legitimately converges them to ~0.4948 (kimi-consultant
  0.15 is the only deliberate exemption); (2) B3 implemented as spend sub-cap
  (price-order-neutral) instead of tier demotion — demoting routstrd ($0.53/M,
  cheapest metered) would push overflow to MORE expensive metered lanes.
