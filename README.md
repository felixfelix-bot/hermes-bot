# Hermes Bot — State & Disaster Recovery

This repo contains ALL stateful configuration, scripts, and plans needed to
fully replicate a Hermes Agent instance from scratch. If the hard drive dies,
clone this repo and follow the setup guide to restore all functionality.

## Structure
- `scripts/net4sats/` — net4sats MVP scripts (autoheal, gateway restart, feed indices)
- `scripts/human-gate/` — Human-gate system (MCP, resolver, digest, Nostr sync)
- `scripts/crons/` — Hermes cron scripts (stale resetter, auto-assigner, anomaly notify)
- `config/` — Configuration files and templates
- `docs/net4sats/` — Plan documents for net4sats MVP
- `repos.txt` — Index of all GitHub repos in the ecosystem
- `synergy_map.json` — Project synergy map

## Recovery
1. Clone this repo
2. Install Hermes Agent (`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`)
3. Copy scripts to `~/.hermes/profiles/manager/scripts/`
4. Set up crons from `config/cron-manifest.json`

5. Source secrets from `nostr-glasses/secrets/.env`
6. Clone kanbanstr fork: `git clone https://github.com/net4sats/kanbanstr`

## Mirror Policy
When stateful files on the local machine change (scripts, configs, plans),
the same changes MUST be committed to this repo. This ensures full replication.

## Pressure FSM — two-layer pressure routing, stage S2b (t_4dfaf0d5)

`zai_proxy.py` now carries a shadow-mode pressure routing layer
(`pressure_fsm.py`, design: merchant-routing-engine
`DESIGN-two-layer-pressure-routing.md`). Shadow mode computes and logs the
routing decision it WOULD make; it never reroutes a live request.

New stateful artifacts (all in this dir):
- `pressure_state.json` — FSM band state (GREEN/AMBER/RED, dwell timer,
  floor-raiser). Written by the tracker; safe to delete (resets to GREEN).
- `pressure_decisions` table in `zai_usage.db` — one row per glm-5.3
  POST: band, interactive flag, would-serve model/provider, reason.
- `pressure_policy.json` (OPTIONAL) — policy overrides, e.g.
  `{"mode": "off"}`. Absent file = defaults (shadow mode).
- `.pressure_routing_disabled` — kill switch flag file. `touch` it to
  disable all pressure computation instantly; delete to re-enable.

Observability: `GET /pressure` on the proxy port returns the current band,
mode, kill-switch status, and last shadow decisions (`?limit=N`, default 20,
clamped to [1,100]). Policy JSON threshold overrides are range-checked —
dwell < 60s is clamped and inverted threshold orderings (a flap machine)
fall back to defaults. Silent model
rewrites now emit `X-Served-Model` / `X-Downgrade-Reason` response headers
(rewrite behavior itself unchanged).

NOTE: the FSM bridge loads at proxy start. After deploying changes to
`zai_proxy.py`, restart the proxy process to activate. The drift-guard
cron does NOT watch zai_proxy.py (only rate_limit_gate.py).

### S2c — live enforcement (t_b82e5665)

`Handler._pressure_enforce` applies the FSM decision when the tracker
runs `mode=enforce`. Scope is deliberately narrow: only the two Ollama
downgrade rows of the decision matrix (`bg_downgraded_olloma`,
`bg_downgraded_ollama_extra` — AMBER/RED background glm-5.3 traffic) are
rerouted to ollama_cloud glm-5.2 (flat-rate; protects the friend key).
Interactive, friend-path, last-resort and non-5.3 decisions are never
touched. If ollama_cloud refuses/fails, the request falls through to the
normal cascade (bg_last_resort semantics) — enforcement can redirect
pressure traffic, never block it. The hook sits AFTER the global spend
cap, so it cannot bypass the runaway-loop circuit breaker.

Enabling / disabling (both hot — no proxy restart needed, the policy
cache re-reads on mtime change):
- Enable: `echo '{"mode": "enforce"}' > ~/.hermes/bot/pressure_policy.json`
- Revert to shadow: `echo '{"mode": "shadow"}' > ~/.hermes/bot/pressure_policy.json`
- Kill switch (all modes): `touch ~/.hermes/bot/.pressure_routing_disabled`
  (+ `systemctl --user restart zai-proxy.service` for belt-and-braces)

Enforcement events are visible as `pressure_enforce_<band>` rows in the
`key_decisions` table (via the `_try_ollama_cloud` reason override);
enforced responses carry `X-Provider: ollama_cloud` and show up as
glm-5.2/ollama_cloud in `api_calls`. `GET /pressure` shows
`mode: enforce` and the live band.

Deployment precondition: >=24h of shadow decisions in `pressure_decisions`
with sane band transitions before flipping to enforce.

### S3a — adaptive tuner revival (t_12f0a395)

`adaptive_model_tuner.py` (weekly cron `0 3 * * 0`, `no_agent`, output
`~/.hermes/profiles/manager/cron/output/f1809b0f26b1/`) now calibrates the
pressure FSM bands, not just the legacy tier thresholds:

- `pressure_policy.json` — `escalate_amber_pct` / `escalate_red_pct` /
  `deescalate_amber_pct` / `deescalate_green_pct` from percentiles of the
  friend-key 5h-window `used_pct_observed` history in `kalman_samples`
  (top 15% of observations → AMBER, top 5% → RED). Guardrails:
  amber ∈ [30,75], red ∈ [amber+10, 95], de-escalate mirrors the compiled
  defaults' symmetry (`deesc_amber == esc_amber`, `deesc_green == esc_amber-15`).
  First calibration (2026-08-17, 305 samples): AMBER ≥ 56%, RED ≥ 92%.
  Predictive thresholds, dwell, floor-raiser etc. are left at FSM defaults —
  the tuner owns only the four used_pct bands.
- Merge-write semantics: foreign keys already in `pressure_policy.json`
  (e.g. `mode: enforce`) are PRESERVED — the weekly cron can never
  silently re-enable or disable routing. Writes are atomic
  (tempfile + rename); the proxy's mtime-cache picks up a new file
  without restart. Fewer than 30 samples → policy write skipped, FSM
  keeps defaults.
- `model_tier_thresholds.json` — legacy output unchanged (router is
  currently unwired: `zai_proxy._select_model_tier` is None), kept for
  compat.
- `model_tier_router.MODEL_MAP` updated to the current generation:
  reasoning=glm-5.3, standard=glm-5.2, economy=glm-4.5-flash.

Run manually: `python3 adaptive_model_tuner.py [--stats | --dry-run]`.
Tests: `python3 -m pytest tests/test_adaptive_model_tuner.py -q` (34 tests;
includes a PressureTracker round-trip asserting the FSM's range-safety
accepts the tuner's bands and that a pre-existing `mode=off` survives a
tuner rewrite).


## Provenance — manager-deployed gate/proxy scripts (2026-08-16)

`rate_limit_gate.py` and `zai_proxy.py` are MANAGER-DEPLOYED from
hermes-scripts origin/master (github.com/felixfelix-bot/hermes-scripts).
Do NOT restore/checkout/stash-apply old committed copies of these files
during bot-dir syncs — that silently reverts the live cron gate to a
pre-T3.1 version that cannot see z.ai timeout/502 bursts (regression
observed twice: 2026-08-15 20:14 IST and 2026-08-16 04:31 IST).

Rules:
- Deploy a new version by committing to hermes-scripts master FIRST,
  then copying into this dir. Both copies must stay byte-identical.
- Watchdog cron `rate-limit-gate-drift-guard` (*/5m, no-agent) hashes
  the live file against hermes-scripts origin/master, auto-heals drift,
  saves forensic copies to `~/.hermes/drift-guard/forensic/`, and
  alerts on drift.
- Deliberate deploy of an uncommitted version: pause that cron or use
  `python3 ~/.hermes/scripts/rate_limit_gate_drift_guard.py --check-only`,
  and finish the hermes-scripts commit within the pause window.
