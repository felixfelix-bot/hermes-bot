# ADR-0014: Model intake pipeline — capabilities are discovered, staged, then gated promotion

**Date:** 2026-09-09
**Status:** ACCEPTED

## Context

The router's model capability surfaces were **manually curated**: `PROVIDER_MODELS`
dict (routing candidates), `zai_proxy._PROVIDER_MODEL_NAMES` (dispatch-time name
translation), and `model_context_registry.json` (context lengths). Every new
upstream model required a human to hand-edit code + registries before it could be
routed. ADR-0009 explicitly deferred automatic capability discovery ("Broader
capability matrix (auto-discovery from /models endpoints...) deferred to future
ADR").

Three operational incidents (see plan `model-intake-staging-2026-08-31.md`)
traced back to **missing semantics, not missing models** — /v1/models is a public
price list, so blanket auto-sync both loses drift signals and risks selling
seed-priced models at a loss (or violating z.ai ToS on quota resale). We needed
model capability to be *discovered* from live provider catalogs, but *promotion
to routing/advertising must remain gated and human-batched.*

## Decision

Adopt a **gated auto-staging intake pipeline**. Discovery is automatic and
continuous via the drift cron; promotion is explicit and human-batched.

1. **Stage store (`model_intake.json`, repo root, git-tracked).** New upstream
   models (live probe, unknown to `PROVIDER_MODELS` and
   `model_context_registry.json`) land in a quarantine stage keyed by canonical
   id. Non-chat modality → `status=rejected` immediately. STAGED models are
   **not routable** and **not advertised**; unknown-model requests keep the loud
   503 (never silent substitution).

2. **Eligibility probe.** Staged chat models get a 1-token (`max_tokens=1`)
   completion probe per provider (≤1 probe per (model,provider) per 6h cron).
   The `model_field` in the probe response must match the requested canonical
   family (catches silent substitution at probe time). ≥2 DISTINCT healthy
   providers passing → `status=eligible`.

3. **Gated promotion overlay (`scripts/model_promote.py`).** `apply <canonical>`
   moves an eligible model to `status=promoted_routing` and overlays it onto the
   routing/advertise surfaces **without runtime source editing**:
   - `flat_router.PROVIDER_MODELS` gains the canonical in each probe-verified
     provider's set (import-time overlay + `refresh_intake_overlay()` hook).
   - `zai_proxy._PROVIDER_MODEL_NAMES` registers provider-native names from probe
     evidence (non-identity mappings carry a dated `# SUBST` commit → audit-green).
   - `zai_proxy /v1/models` advertises the model **only if** (a) ≥1 healthy
     non-z.ai provider exists (tier wall — z.ai/ours/friend/manager/worker keys
     all count as z.ai, **never public**) AND (b) a measured price exists
     (`real_price_tracker` n≥50). Seed/estimated rates never advertise.
   - Kill switch `.disable_intake_overlay` skips the overlay (revert = rm +
     restart; permanent rejection = `deny <canonical>` in the store).

4. **Removal grace (never auto-remove routing).** A model absent from ALL live
   provider catalogs gets `missing_since` set and `status=grace`, with an alert
   line in the drift report. Grace >7 days:
   - non-promoted entry → **dropped** from the store,
   - `promoted_routing` entry → **kept** and listed in the human digest
     (REMOVALS section) for **manual** registry removal — routing entries are
     never auto-removed (a phantom auto-removal would silently break dispatch).
   - A model that reappears during grace has its pre-grace status restored.

5. **Digest stdout.** The drift-cron stdout gains `PROMOTION BATCH:` (eligible
   models: canonical, provider breadth, measured-price y/n) and `REMOVALS:`
   sections. Both are **omitted when empty**, so the drift cron keeps its
   empty-stdout-when-clean contract.

## Consequences

- New models are discovered automatically but only become routable after an
  explicit human `apply` — no accidental silent-routing of untrusted upstream
  models.
- Advertising is doubly gated (tier wall + measured price), so no z.ai-backed or
  seed-priced model appears on the public price list.
- Removal is safe: staged/dropped models are evicted automatically after a 7-day
  grace and alert, but live routing entries require a human removal step — the
  router never stops dispatching a model that operators are still selling.
- The drift cron stays silent-when-clean (both digest sections empty), so
  monitoring noise is unchanged for the clean steady state.

## Related
- ADR-0009 (capability-aware routing — PROVIDER_MODELS as source of truth;
  auto-discovery deferred here)
- `docs/flat-router-design.md` §2.6 + capability-surfaces section
- Plan: `model-intake-staging-2026-08-31.md`
