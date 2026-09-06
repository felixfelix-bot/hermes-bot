# Benchmark-tier model resolution — plan + checklist

Date: 2026-09-06 (Phase 1 shipped, shadow mode)
Design owner: operator session; approved decisions inline.

## Objective
Profiles pin a **tier alias** (e.g. `tier/coding-worker`) instead of a concrete
model. The proxy resolves each alias to the **cheapest healthy model meeting
the tier's minimum benchmarks** — hybrid benchmark source (curated capability
floor + live fleet gates), cheapest-with-stickiness-and-hysteresis, workers
first, manager last, few shared tiers.

Replaces today's manual pinning reflex (e.g. the 2026-09-06 morning flip of
manager/treasurer to `deepseek/deepseek-v4-flash` during the z.ai storm).

## Approved decisions (from the planning Q&A)
- **Benchmarks:** hybrid — curated capability floor (context window, coding
  class, tool-use, evidence-tagged) PLUS live fleet gates (provider health,
  garbage demotion, trailing empty-completion rate). `context_length` is a
  first-class filtered minimum.
- **Arbitration:** cheapest + **per-session stickiness** (never switch models
  mid-conversation) + **hysteresis** (switch only when the incumbent is
  unhealthy or a challenger is < 75% of the incumbent's cost).
- **Rollout:** shadow first (>= 3 days) → low-risk workers → worker fleet →
  manager LAST (it is the path we just recovered).
- **Registry:** few shared tiers, one YAML, profiles reference a tier name.
- **Resolution point:** the proxy (`zai_proxy.py` + `flat_router.py`), NOT
  hermes-agent — hermes passes the alias string through untouched (validated
  empirically, see Evidence).

## Architecture

```
profile config model.default: "tier/coding-worker"
  → hermes-agent sends model string verbatim (validated via `hermes -m`)
  → zai_proxy hook (_proxy, before the pressure FSM):
      alias? → flat_router.resolve_tier(alias, X-Hermes-Session)
                ├─ capability filter (model_benchmarks.yaml)
                ├─ live gates: empty-rate window, then
                │   select_provider(model) per candidate — reuses the
                │   REAL walk (health, garbage, phantom, Kalman cost,
                │   exhaust weight) so resolution can never drift
                │   from routing
                ├─ stickiness: session → (model, cost, TTL 24h)
                ├─ hysteresis: switch iff challenger < 0.75 × incumbent
                ├─ fallback: empty pool → fallback_model (never 503)
                └─ kill-switch: .disable_tier_resolution → instant pin
      rewrite request body "model" → resolved concrete model
  → normal provider walk + failover (unchanged)
  → tier_resolutions row (mode=live)
Concurrently every request: shadow_tier_evaluate() — per-tier "what would
have resolved" log (rate-limited 5 min/tier), mode=shadow, zero routing
effect. Flipping `mode:` to live in model_tiers.yaml stops the periodic
shadow logging (real resolutions keep logging mode=live).
```

Concrete model strings are NEVER alias-resolved — hard pins keep working
forever (escape hatch). `.emergency_ollama_only` / `.disable_flat_router`
make tier resolution fall to the tier pin WITHOUT consulting the router.

## Inventory
| File | Role |
|---|---|
| `model_tiers.yaml` | tier registry: minima, candidate allowlists, fallback pins, knobs |
| `model_benchmarks.yaml` | per-model capability scores w/ provenance, `verify_context_window` flags |
| `flat_router.py` | `resolve_tier`, `shadow_tier_evaluate`, gates, stickiness, kill-switch |
| `zai_proxy.py` | `_tier_alias_resolve` + `_tier_resolution_shadow` hooks in `_proxy` |
| `test_flat_router.py` | `TestTierResolution` (20 hermetic tests) + Phase0 shadow patches |
| `zai_usage.db::tier_resolutions` | full audit trail (mode, tier, resolved, reason, considered, cost, session) |

## Runbook
- **Instant pin:** `touch ~/.hermes/bot/.disable_tier_resolution` — every tier
  resolves to its fallback_model within 30s, including sticky sessions. Delete
  the file to re-enable.
- **Watch resolutions:**
  `sqlite3 ~/.hermes/bot/zai_usage.db "SELECT mode,tier,resolved_model,reason FROM tier_resolutions ORDER BY ts DESC LIMIT 20"`
- **Flip a profile (Phase 2):** back up config, edit `model.default` (+
  `delegation.model` for manager) to `tier/...`, set `context_length:` to the
  tier's min_context (keeps hermes compression math truthful). Rollback =
  restore the backup.
- **Add a model:** add a benchmark entry (evidence required!), then add it
  to the relevant tier `candidates` allowlists. mtime-cached — live in 0s.
- **Tune:** all knobs live in `model_tiers.yaml` (hysteresis ratio, TTLs,
  empty-rate window, shadow interval). No restarts needed for data changes;
  resolver code changes need `systemctl --user restart zai-proxy`.

## Evidence (Phase 1 validation, 2026-09-06 ~15:00)
- E2E canary: `HERMES_PROFILE=treasurer hermes -m "tier/coding-worker" -z "Reply with exactly: tier-ok"`
  → **"tier-ok"** (resolved `deepseek/deepseek-v4-flash`, live row logged).
  Proves hermes-agent pass-through (the key Phase-2 unknown).
- Shadow rows flowing for all 4 tiers on live traffic (cheapest_qualified);
  `/quota`-style sanity: shadow picks vary with the provider-disable window
  (see Caveats), as designed.
- Suite: `test_flat_router.py` 109/109; full-suite diffs vs stashed-HEAD
  control = ambient/flaky only (oc3 scarcity pair flakes on re-run; ctx-gov /
  response_parsing / retry_tracking / ppq failures reproduce identically
  without these changes — the known ambient-state class, board t_303cc82e).

## Caveats
- Benchmark context windows are FLEET-ASSUMED (profile-config values), not
  provider-verified — kimi entries flagged `verify_context_window: true`.
  Verify via canary before trusting tight tier filtering.
- Shadow soak started inside a lane-maintenance window (`.key_disabled_ours/
  friend/ollama_cloud*/opencode_go` placed 07:22 by the manager during the
  Chutes wiring) — shadow picks are over a reduced provider pool until
  those clear. Fine for contract validation; re-read costs after the pool
  recovers.
- Empty-rate gate fails open under 50 samples — canaries pre-qualify NEW
  cheap models; the gate catches SICK incumbents (exactly the 07:05
  glm empty-storm signature).
- The friend-key mystery (t_69da44d9) gates glm-family tier candidates: until
  restored, glm tiers resolve via remaining lanes.

## Checklist
### Phase 1 — data + resolver + shadow (this session)
- [x] Design + operator decisions (hybrid benchmarks, stickiness+hysteresis,
      workers-first, shared tiers, proxy-side resolution)
- [x] `model_benchmarks.yaml` — 8 models, evidence-tagged, stale_after_days
- [x] `model_tiers.yaml` — 4 tiers (manager-min, worker-heavy, coding-worker,
      consultant) + all knobs + kill-switch contract
- [x] `flat_router.resolve_tier()` — capability filters, live gates (reusing
      `select_provider` per candidate), stickiness, hysteresis, fallback,
      kill-switch, emergency-flag pinning
- [x] `flat_router.shadow_tier_evaluate()` — rate-limited, read-only
- [x] zai_proxy hooks: live alias resolution + shadow evaluation + request
      body rewrite; module-level wrappers patchable like `_pressure_shadow`
- [x] `tier_resolutions` table (audit trail)
- [x] 20 hermetic tests (`TestTierResolution`) — no DB/state/yaml coupling
- [x] Phase-0 test isolation patches for the new shadow hook (convention:
      patch `_tier_resolution_shadow` like `_pressure_shadow`)
- [x] Full-suite control run — zero regressions attributable to this change
- [x] Proxy restarted; shadow rows flowing on live traffic
- [x] E2E canary (hermes `-m tier/...` one-shot) — pass-through validated
- [x] Plan doc (this file) committed via branch dance
- [x] Board task filed (router-maintenance)

### Phase 1.5 — shadow soak (>= 3 days)
- [ ] Daily review of tier_resolutions vs actual routing (drift, flapping,
      empty_pool events, surprise picks) — add to morning checks
- [ ] Verify kimi-k3 / kimi-k2.7-code context windows (canary)
- [ ] Refresh benchmark scores from canary evidence after the provider pool
      recovers (disabled-lane window closes)
- [ ] Confirm hermes-agent tolerates long-lived tier aliases in live profiles
      (cron restart cycles) — passive observation during Phase 2a

### Phase 2 — workers live (staged, gated on clean soak)
- [ ] Set `mode: live` in model_tiers.yaml (stops periodic shadow logging)
- [ ] 2a: treasurer + worker-pae + worker-data → `tier/coding-worker`
      (config backups `config.yaml.pre-tier-20260906`); watch kanban task
      success + cost/task + empty rate for 48h
- [ ] 2b: worker-inspector/plebeian/merger → `tier/worker-heavy` /
      `tier/coding-worker`; remaining worker-* fleet incl. spawned
      worktree-profiles (update the spawn template so new ones inherit)
- [ ] Rollback path verified live once (kill-switch + config restore drill)
- [ ] worker-reviewer-*, kimi-consultant, vision stay pinned BY DESIGN
      (documented in model_tiers.yaml)

### Phase 3 — manager (>= 1 week clean Phase 2)
- [ ] manager: `model.default` AND `delegation.model` → `tier/manager-min`
- [ ] Watch DM responsiveness + delegation behavior for a few days
- [ ] Post-rollout report: resolution history audit + cost delta vs pins
