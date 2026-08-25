# Plan: Regime-Shift Alerts + Price/Quota Visualizations

**Date:** 2026-08-26
**Status:** EXECUTING
**Severity:** Medium — surface quota↔metered transitions without spamming

## Problem

When traffic shifts from quota-based providers (z.ai, ollama, opencode_go) to pay-per-token providers (routstrd, neuralwatt, ppq), it costs $19+/day. The existing alerts fire reactively (after the bleed). We need proactive detection of the *trend* itself, with anti-spam guards, plus visualizations to see the price landscape.

## Checklist

### Phase 1 — ADR + Regime-Shift Detection
- [ ] Write ADR-013: regime-shift Kalman alerting
- [ ] Implement regime-shift check in cost-escalation-check.py
- [ ] Unit test: synthetic Aug 23→24 collapse → exactly 1 up-alert, 0 duplicates
- [ ] Unit test: recovery → exactly 1 down-alert

### Phase 2 — Price/Quota Visualizations
- [ ] price_viz.py: data layer (routing_profit, provider_balances, kalman_samples)
- [ ] price_viz.py: V1 2D price-vs-quota envelope curves (LOG/LINEAR toggle)
- [ ] price_viz.py: V2 price heatmap (time×provider, LogNorm)
- [ ] price_viz.py: V3 quota heatmap (time×provider, linear 0-100%)
- [ ] price_viz.py: V4 3D surface (session×weekly→price, per provider)
- [ ] price_viz.py: V7 ASCII block for Signal/terminal

### Phase 3 — Endpoint + Cron
- [ ] zai_proxy /viz/*.png static handler
- [ ] Hourly render cron
- [ ] Transition-only PNG push on regime shift

### Phase 4 — Commit + Push
- [ ] Commit ADR + regime-shift check
- [ ] Commit price_viz.py + tests
- [ ] Commit zai_proxy endpoint + cron
- [ ] Push all repos

## Rollback
- `touch ~/.hermes/bot/.disable_regime_alerts` — kills regime-shift push
- `touch ~/.hermes/bot/.disable_viz_endpoint` — kills /viz/* serving
- All changes additive; no existing paths modified except insertion points