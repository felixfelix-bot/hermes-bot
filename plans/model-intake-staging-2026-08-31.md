# Model Intake — Gated Auto-Staging (2026-08-31)

Status: APPROVED by Felix ("Go", Signal hermes-admin-setup 2026-08-31).
Consult verdict: reject blanket auto-add; adopt gated auto-staging (2× glm-5.3 consult).

## Design (fixed, consultant-approved)

New upstream models discovered by the drift cron go to a QUARANTINE STAGE
store. They are NOT routable (flat router), NOT advertised (/v1/models).
Unknown-model requests keep the loud-503 rule (commit 200b703) — never
substitution. Promotion is gated, human-batched.

Pipeline: drift cron → STAGE → modality gate → 1-token probe → canonical
merge across providers → eligible (≥2 healthy providers, FR-C rule) →
human promotion batch → routing overlay (+advertise only w/ measured price
AND ≥1 healthy non-z.ai provider — TIER WALL: z.ai-backed models NEVER
public; z.ai ToS quota-resale) → removal = 7d grace + alert, no auto-removal
of routing entries.

Why (4 consult reasons): (1) /v1/models = public price list — seed-priced
models = sell-at-loss + z.ai ToS violation. (2) blanket auto-sync kills
drift detection (registry always "clean"). (3) name fragmentation →
single-candidate models → 503 storms. (4) embedding/TTS noise +
substitution re-arm. All 3 Aug incidents = missing semantics, not missing
models.

## State store: model_intake.json (repo root, git-tracked)

Keyed by canonical id (reuse `to_canonical()` from catalog_drift_check.py):

```json
{
  "deepseek/deepseek-v5": {
    "raw_ids": {"opencode_go": "deepseek-v5", "neuralwatt": "deepseek/deepseek-v5"},
    "modality": "chat",
    "status": "staged|rejected|eligible|promoted_routing|grace",
    "first_seen": "ISO", "last_seen": "ISO", "missing_since": "ISO|null",
    "probes": {"opencode_go": {"ts": "ISO", "pass": true, "http": 200, "model_field": "deepseek-v5"}},
    "advertised": false,
    "decided_by": "human|auto-rule", "decided_at": "ISO"
  }
}
```

## Task chain (merchant-routing board, sequential links)

### INTAKE-1 — Stage store + drift-checker intake wiring
- catalog_drift_check.py: on NEW upstream model (live probe, unknown to
  registry + PROVIDER_MODELS): write staged entry. Non-chat modality →
  status=rejected immediately (reuse `_is_chat_model`).
- STAGED = quarantine: no PROVIDER_MODELS entry, no /v1/models entry.
  Verify: request for staged model → 503 (existing rule, unchanged).
- Idempotent re-runs; update last_seen; log to catalog_drift.log.
- Tests: new upstream → staged; embedding model → rejected; existing
  model → no entry; rerun idempotent.

### INTAKE-2 — 1-token probe + cross-provider merge + eligibility
- Probe staged (chat) models per provider: max_tokens=1 completion,
  record pass/http/model_field (model_field must match requested canonical
  family — catches silent substitution at probe time).
- Probe budget: ≤1 probe per (model, provider) per cron run (6h). Never
  probe non-chat.
- Eligibility: ≥2 DISTINCT providers w/ pass=true → status=eligible.
  Else stays staged (probes retried next run, fail evidence kept).
- Tests w/ mocked provider endpoints: 2 passes → eligible; 1 pass →
  staged; model_field mismatch → probe fail.

### INTAKE-3 — Promotion CLI + routing/advertise overlay + tier wall
- scripts/model_promote.py: `list` (human digest: eligible batch + price
  status + provider breadth), `apply <canonical>` , `deny <canonical>`.
- Overlay pattern (NO source-code editing at runtime): flat_router.py
  loads promoted entries from model_intake.json into PROVIDER_MODELS at
  import + on refresh; zai_proxy /v1/models overlays advertised entries.
  Raw-id dispatch translation via existing _PROVIDER_MODEL_NAMES pattern
  (register provider-native name from probe evidence; SUBST-marker audit
  test must stay green).
- apply → status=promoted_routing. advertise flag set TRUE only if:
  (a) ≥1 healthy NON-z.ai provider (zai/ours/friend/manager/worker key
  names all count as z.ai — tier wall, NEVER public), AND
  (b) measured price exists (real_price_tracker n≥50 — else advertised
  stays false, retry on later runs; seed/estimated NEVER advertises).
- Kill switch: .disable_intake_overlay → overlay skipped (revert = rm +
  restart, plus deny in store for permanence).
- Tests: overlay loads/dies w/ switch; tier wall (z.ai-only model never
  advertised); unmeasured price never advertised; failure-injection —
  dominant provider unhealthy, candidate breadth ≥2 asserted + response
  model field correct.

### INTAKE-4 — Removal grace + digest + ADR + docs
- Model absent from ALL provider catalogs → missing_since set, status=grace,
  alert line in drift report. Grace >7d → dropped from store; if it had
  promoted_routing status → listed in human digest for manual registry
  removal (never auto-removed from routing).
- Digest: drift cron output appends "PROMOTION BATCH:" section (eligible
  models w/ provider breadth + price status) and "REMOVALS:" section.
  Delivered via existing cron stdout → catalog_drift.log + Signal digest
  wrapper (reuse cost-digest time-gate pattern if separate cron needed).
- ADR: check docs/decisions.md for "capability = discovered, not
  hardcoded" — extend w/ intake pipeline; else add ADR-0xx.
- Update SKILL references (flat-router-architecture) — manager handles
  skill patch; worker updates repo docs only.

## Constraints for ALL tasks

- Worktree: ~/worktrees/model-intake, branch worker-routing/model-intake,
  off current main HEAD. COMMIT ONLY YOUR FILES — ~/.hermes/bot has
  another session's dirty files (test_flat_router.py, pressure_policy.json,
  model_tier_thresholds.json, handovers/INDEX.md, zai_proxy.py.bak D,
  zai_proxy_state.json) — DO NOT touch/stage/commit/revert them.
- Tests: pytest tests/ -k intake (new) + full test_flat_router.py suite
  green. TDD: red first.
- Push branch to all configured remotes (dual-push pattern). No main push.
- Deploy/restart proxy = MANAGER ONLY, after cold review. Standalone cron
  scripts (catalog_drift_check.py) go live only after merge to bot main.
- Never return HTTP 200 w/ different model (existing law). Never probe w/
  max_tokens>1. Never advertise z.ai-backed or unmeasured-priced models.
