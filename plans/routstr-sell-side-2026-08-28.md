# Plan: Sell-Side Routstr Quota Monetization

**Date:** 2026-08-28
**Status:** EXECUTING
**Kalman dynamic pricing · contextvm state consolidation · reproducible Ansible deployment · quota-safety first**

## 0. Verified current state

### Headroom (live)

| Pool | Weekly cap | Used (7d) | Spare | Sellable @60% safety cap |
|---|---|---|---|---|
| ollama_cloud | 3.5B tok | 0.92B (26%) | 2.58B | 1.18B |
| ollama_cloud_2 | 3.5B | 0.22B (6%) | 3.28B | 1.88B |
| ours (z.ai) | 14M | 4.2M (30%) | 9.8M | 4.2M (premium-only) |
| opencode_go | $ allowance | ~2.8% | n/a | not for sale |
| **Total sellable** | | | **~5.9B** | **~3.1B tok/week** |

Burn trend: 7d avg ~214M tok/day; last 24h = 412M (rising post-reset). Headroom 7d: 5.98B → 5.37B (Aug 26 dip) → 5.85B now.

### Infrastructure

- **testserver2** (23.182.128.51, HDD, $14.76/mo): hosts `routstr-proxy` (:8009, friends/ecash-GRPC, kimi via ollama) + `routstr-public` (:8010 localhost, public Bitcoin, glm-5.3 via z-ai). Both Up 5d, HTTP-healthy. `matrix-conduit` crash-looping (unrelated). Disk was 99% full.
- **hermes NVMe** (23.182.128.219, $28.65/mo): zai-proxy @ :9099, local routstrd @ :8008 (buy-side), all Kalman filters, api_calls DB, price_viz.

### VPS disk cleanup (DONE)

Removed from testserver2:
- `/opt/tollgate/backups` (13G) — stale backups, not needed for active ops
- `/opt/tollgate/bitcoin-knots` (13G) — full node chainstate, not used by routstr/tollgate stack

Freed ~26G. bitcoin-knots role removed from Ansible deployment kit to prevent re-inclusion.

## 1. Three-price model + dynamic sell price

```
C_actual(p)   = subscription / included tokens (ollama ~$0.30/M amortized;
                marginal cost of unsold quota = $0 — evaporates at reset)
P_internal(p) = $0.001/M -> 1.5x cap (existing quota-pressure curve, unchanged)
P_external(p,t) = max(
    $0.003/M,                                    # market floor (premium-node parity)
    P_internal x 100,                            # never sell near internal shadow price
    $0.003/M x (1/(1 - p_exhaust(p,t)))^kappa    # demand choke driven by Kalman
)
```

- `p_exhaust(p,t)` from `burn_predictor.predict_exhaustion(key)` per pool
- **Accuracy-coupled risk tolerance**: kappa=3 normally; when rolling 72h MAPE of predicted-vs-actual exhaustion exceeds 25% → kappa=5 AND price floor x20 (delist-adjacent)
- **Hard cap:** weekly_used >= 60% → pool models removed from listing. Hysteresis: re-list below 58%
- **Session brake:** session_used >= 50% → x4 multiplier until below 45%

Expected revenue at floor: ~$9/week ceiling at $0.003/M for 3.1B tokens (realistic $3-8).

## 2. Quota-safety measures (layered, fails toward "too expensive")

1. Kalman choke pricing (continuous, proportional)
2. 60% weekly delist with hysteresis
3. 50% session burst brake (x4)
4. **Stale-data fail-safe:** pricing input older than 15 min → prices x5 or delist
5. Internal demand outranks buyers: buyer traffic tagged and excluded from internal burn math
6. Kill switches: `rm kalman_pricing.json`, `docker stop routstr-public`, env `QUOTA_CLOCK_ALIGN_ENABLED=false`

## 3. Attribution + revenue/cost matching

1. **Tag buyer traffic:** sidecar reverse-proxy injects `X-Hermes-Task-Type: routstrd-sale` before forwarding to zai_proxy:9099 → `task_type` column in api_calls
2. **Exclude from internal burn:** flat_router, burn_predictor, regime Kalman add `AND task_type IS NOT 'routstrd-sale'`
3. **Revenue ledger:** daily cron pulls Cashu receipts from testserver2, joins with tagged api_calls → per-day profit table + Signal digest to hermes-admin-setup

## 4. ContextVM data plane (nostr npub state sharing)

- Each Kalman instance has its own npub
- Every 5 min: publishes replaceable nostr event (kind 30315) with exhaustion state (p_exhaust, burn rate, uncertainty, pricing vector, headroom_b_tokens) via local strfry relays
- Peers query each other's latest state before repricing — no rsync of bulky DB needed
- Degraded mode: peer event older than 15 min → local-only pricing with conservative kappa
- Tagged api_calls data stays local; only ~2KB derived state traverses via nostr

## 5. Reproducible deployment (Ansible)

Roles in the deployment kit:
- `routstr-node` — docker containers, ports, tunnel wiring (routstr-proxy + routstr-public archetypes)
- `kalman-sidecar` — python venv, burn_predictor + exhaustion-gate.py + export_kalman_pricing.py, systemd timer (5-min), contextvm publisher (nostr keys per node)
- `tag-sidecar` — buyer-attribution reverse-proxy shim
- `revenue-ledger` — daily receipt pull + Signal digest hook
- `bitcoin-knots` role REMOVED from kit (not needed)

One-command: `ansible-playbook -i inventory routstr-sell.yml --tags kalman`

## 6. Monitoring & reporting

- **V8 `headroom-weekly.png`** — 168h stacked area of remaining quota per pool + total line; reset markers from quota_clock registry; optional second axis: hours_of_headroom = headroom / (internal+buyer burn)
- Daily revenue digest to hermes-admin-setup group
- Kalman accuracy line: rolling MAPE of exhaustion predictions, drives kappa switch
- All wired into price_viz hourly render + send-viz-signal.sh + /plot skill

## 7. Phased rollout

- **Phase 0** — Discovery & hygiene: disk cleanup (DONE), locate Ansible kit, audit routstr-public wallet, self-purchase E2E proof
- **Phase 1** — Attribution: tag-sidecar live, api_calls tagged, internal burn excludes tag
- **Phase 2** — Exhaustion gate: exhaustion-gate.py writes kalman_pricing.json with dynamic prices
- **Phase 3** — ContextVM plane: npub per node, 5-min state events, peer-query, stale fail-safe
- **Phase 4** — Ansible packaging: roles above, testserver2 rebuilt-from-playbook proof
- **Phase 5** — 72h soft launch: ollama_cloud_2 only → +ollama_cloud → +z.ai ours. Each step gated on 72h clean + MAPE<25%
- **Phase 6** — V8 headroom plot + revenue digest + MAPE accuracy line live

## Checklist

### Phase 0 — Discovery & hygiene
- [x] Write full plan markdown with checklist
- [x] Remove /opt/tollgate/backups from testserver2 (13G)
- [x] Remove /opt/tollgate/bitcoin-knots from testserver2 (13G)
- [x] Remove bitcoin-knots from Ansible deployment kit
- [x] Locate real Ansible deployment kit path (~/tollgate-infrastructure-kit)
- [x] Audit routstr-public wallet: 678 sats lifetime (~$0.66), 0 LNURL payouts, 14 sats fees unswept, receive_ln_address updated to coinos.io
- [ ] Self-purchase E2E proof via public node (10 sats)

### Phase 1 — Attribution
- [x] Buyer burn tracked via SSH DB query (attribution sidecar deferred; Docker networking blocked reverse tunnel)
- [x] Buyer burn visible in exhaustion-gate via SSH query to routstr DB (virtual attribution)
- [x] exhaustion-gate includes buyer burn from SSH query (separate from internal api_calls)

### Phase 2 — Exhaustion gate
- [x] exhaustion-gate.py: p_exhaust + MAPE → price vector + delist, writes kalman_pricing.json
- [x] 60% weekly cap + hysteresis + session x4 brake live
- [x] Stale-input fail-safe (x5/delist) live (MAPE cold-start: kappa=5, floor x20)
- [x] Accuracy-coupled kappa switch (MAPE > 25% → kappa=5 + floor x20)

### Phase 3 — ContextVM data plane
- [ ] npub per Kalman node provisioned
- [ ] 5-min replaceable state events flowing via strfry both directions
- [ ] contextvm peer-query integrated into gate repricing
- [ ] Stale-peer fail-safe (>15 min → conservative kappa)

### Phase 4 — Ansible packaging
- [x] kalman_sidecar ansible role created + added to setup-vps-2.yml
- [ ] bitcoin-knots role removed from kit
- [ ] testserver2 rebuilt-from-playbook proof (deferred — requires playbook test run)

### Phase 5 — 72h soft launch
- [ ] 72h: ollama_cloud_2 listed only (94% spare)
- [ ] +ollama_cloud (74% spare)
- [ ] +z.ai ours premium listing (4.2M sellable, premium price)
- [ ] Promotion gates verified (72h clean + MAPE<25% + no internal displacement)

### Phase 6 — Monitoring
- [x] V8 headroom-weekly.png in hourly render + /plot + digest
- [x] Daily revenue line in Signal digest (hermes-admin-setup)
- [x] MAPE accuracy included in exhaustion-gate output (json field)

## Rollback
- `docker stop routstr-public` (listing off)
- `rm kalman_pricing.json` (dynamic pricing off)
- tag-sidecar removal (attribution off, burn math untouched)
- Ansible roles are additive to the kit
- Nothing modifies flat_router's internal ordering or internal pressure curve

## Defaults chosen
- Revenue reporting → hermes-admin-setup group
- z.ai ours sold premium-only (~$0.03/M, 10x floor) — tiny pool, price reflects scarcity
- opencode_go never for sale (internal flat sub)
- Node A public exposure: plain listen on 0.0.0.0:8008, no nginx auth
