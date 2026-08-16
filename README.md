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
