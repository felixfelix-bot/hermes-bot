# Viz Endpoint Coverage Survey — Plan

Date: 2026-09-09

## Problem

The viz plots (`price_viz.py`, run hourly by `# hourly-price-viz`) render from
hand-copied static tables that cover only **12** endpoints:

- `price_viz.py:50` `PROVIDER_TIER` (12)
- `price_viz.py:72` `SEED_RATES` (12)
- `price_viz.py:92`/`110` colors & linestyles (12)
- `price_viz.py:527` `LANE_REGISTRY_STATIC` (9)

The live router `flat_router.py` routes **15** endpoints
(`PROVIDER_MODELS:133`, `_SEED_RATES:272`, `PROVIDER_TIER:313`). Endpoints added
to the router but never to the viz (`ollama_cloud_3`, `chutes`, `deepseek`) never
appear in the envelope curves, the `PRICE LANDSCAPE` ASCII bars, or headroom
panel A. `catalog_drift_check.py:53` `PROVIDERS` has the same stale-list problem
for its probes. New proxy lanes (`ollama_cloud_4`, `telnyx_test`) are invisible
too.

## Solution

Make the viz self-heal: every 5th run of the 6-hourly catalog-drift cron
(`10 */6 * * * # catalog-drift-check`) runs a **coverage survey** that compares
the live endpoint universe against what the plots will draw, auto-adds missing
endpoints into a persistent overlay the viz reads at render time, re-renders the
plots, and alerts on what changed.

## Decisions (confirmed with operator)

1. Survey anchored to the **catalog-drift cron (6-hourly)**; every 5th run ≈ 30h.
2. Endpoint universe = **router + proxy lanes** (routed providers + `/quota`
   payload lanes + `real_price_tracker` lanes).
3. Behavior = **auto-add + render + alert** (self-healing).

## Changes

1. `price_viz.py` — load `~/.hermes/bot/viz_provider_overlay.json` at import and
   merge entries into the static dicts; add a deterministic display fallback for
   any entry missing color/linestyle.
2. New `bot/src/viz_coverage_survey.py` — build live universe, compute
   missing-vs-represented, derive metadata (tier/seed/kind/capacity/color/
   linestyle), persist overlay, re-render, alert (deduped).
3. `catalog_drift_check.py` — run counter state file; call the survey when
   `runs % 5 == 0`, wrapped in try/except so drift job never breaks.
4. Tests — `bot/src/test_viz_coverage_survey.py`.

## Checklist

- [ ] Write plan markdown (this file)
- [ ] `price_viz.py`: overlay load + merge into `PROVIDER_TIER`/`SEED_RATES`/
      `PROVIDER_COLORS`/`PROVIDER_LINESTYLES`/`LANE_REGISTRY_STATIC`
- [ ] `price_viz.py`: `_derive_display()` fallback for color/linestyle
- [ ] `viz_coverage_survey.py`: `live_universe()`
- [ ] `viz_coverage_survey.py`: `represented()` + `missing()`
- [ ] `viz_coverage_survey.py`: metadata derivation + deterministic color/linestyle
- [ ] `viz_coverage_survey.py`: `persist_overlay()` + `render_now()` + `run()`
- [ ] `catalog_drift_check.py`: every-5th-run trigger + counter state
- [ ] `test_viz_coverage_survey.py`: diff, color stability, round-trip, gating
- [ ] Dry-run survey; verify `ollama_cloud_3`/`chutes`/`deepseek` appear in plots
- [ ] Run existing viz/router tests to confirm no regression
