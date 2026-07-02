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
