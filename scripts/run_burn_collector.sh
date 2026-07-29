#!/bin/bash
# run_burn_collector.sh — Wrapper that sources .env before running collector
# Ensures PPQ_API_KEY and other env vars are available in cron context
set -euo pipefail

if [ -f "$HOME/.hermes/profiles/manager/.env" ]; then
    set -a; source "$HOME/.hermes/profiles/manager/.env"; set +a
fi

exec /usr/bin/python3 "$HOME/.hermes/bot/api_burn_collector.py"
