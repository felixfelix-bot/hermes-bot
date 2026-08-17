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
