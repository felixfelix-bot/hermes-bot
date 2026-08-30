# Plan: Catalog Findings Curation + Checker Correctness Fixes

**Date:** 2026-08-30
**Status:** EXECUTING
**Trigger:** Baseline drift report verification revealed false-positive classes in
  catalog_drift_check.py + the runtime phantom guard (both shipped 2026-08-30)

## Problem statement
The baseline drift report mixes true findings with three false-positive classes:
1. **Case sensitivity** — telnyx serves capitalized vendor IDs (`Kimi-K3`, `GLM-5.2`);
   canonical lowercase forms (`kimi-k3`, `glm-5.2`) fail the membership check →
   the runtime guard is currently FALSE-BLOCKING telnyx candidates (production
   routing regression: telnyx + routstrd deepeek rungs out of rotation)
2. **Tag-form gap** — _TAG_TRANS covers `deepseek-v4-flash:0731` (colon) but
   routstrd's catalog uses dash-forms (`deepseek-v4-flash-0731`)
3. **Noise** — deepinfra's 186 "missing rungs" are TTS/embedding/audio models;
   ollama "translation gaps" flag verbatim-served models

## User decisions (inherited)
- Curation for new models: report first, registry edits verified live
- Runtime guard: keep, but it must not over-block (fail-open on ambiguity)

## Checklist

### Phase F — Checker/guard correctness (FIRST)
- [ ] F1: `to_canonical` — case-insensitive reverse-map + lowercase normalization
  fallback before flagging unknown
- [ ] F2: tag translation accepts both `:0731` and `-0731` forms (derive both
  separators from _TAG_TRANS)
- [ ] F3: translation-gap only flagged when the provider's raw catalog uses a
  DIFFERENT name than canonical (verbatim providers exempt)
- [ ] F4: routstr/routstrd marked `catalog_complete: false` (marketplace
  aggregators — listing under-reports what they proxy through) → guard
  fail-opens them; phantoms stay informational
- [ ] F5: chat-model filter for missing-rungs (drop TTS/embedding/audio/vector
  families: BAAI/, Bria, Audio*, whisper, *-tts, *embedding*, *vl-/vision-only)
- [ ] F6: opencode_go probe — fix header/URL (was error in baseline)
- [ ] F7: re-run → clean baseline report + regenerated live_catalog_state.json
- [ ] F8: guard regression — telnyx kimi-k3/minimax-m3/gpt-5 PASS;
  routstrd deepseek PASS; detected true phantoms still blocked; 80/80 tests

### Phase G — Registry curation from clean findings
- [ ] G1: PROVIDER_MODELS additions (quota-tier, zero marginal cost):
  - z.ai ours/friend: glm-4.7, glm-5, glm-5-turbo, glm-5.1, glm-5.3-flash
  - ollama_cloud + ollama_cloud_2: glm-5.1, gpt-oss:20b, kimi-k2.6,
    minimax-m2.7, mistral-large-3:675b, nemotron-3-nano:30b,
    nemotron-3-super, nemotron-3-ultra
  - neuralwatt: kimi-k2.7-code ($0.95/$4.00 — cheaper paid rung than kimi-k3)
- [ ] G2: model_context_registry.json additions using snapshot-derived values
  (extend checker to capture context_length/context_window per model):
  glm-5.3-flash, minimax-m3, glm-4.5, glm-4.6v, kimi-k2.5, gpt-5,
  claude-haiku-4-5 + any new rungs from G1
- [ ] G3: telnyx GLM models stay OUT (operator cost decision $12/M — documented,
  not drift)
- [ ] G4: run test_flat_router.py + guard tests after curation

### Phase H — Ship
- [ ] H1: commit + push hermes-bot
- [ ] H2: plan checklist DONE + signal note to hermes-admin-setup

## Key evidence (verified 2026-08-30)
- telnyx live catalog HAS moonshotai/Kimi-K2.5/K2.6/K3 — reverse-map exists in
  zai_proxy:683-689 but to_canonical missed case-insensitive matches
- routstrd :8008 catalog has deepseek-v4-flash-0731 (dash form)
- z.ai serves-but-unlists: glm-4.6v (live-verified 08-27), glm-4.5-flash
  (5-token 200 verified 08-30)
- opencode_go served glm-5.3 in manual probe (33 models) but drift probe
  errored — header issue to fix

## Rollback
- All F-fixes are inside catalog_drift_check.py (delete/restore file)
- G1/G2 are additive entries (revert commits)
- Guard behavior unchanged in fail-open posture