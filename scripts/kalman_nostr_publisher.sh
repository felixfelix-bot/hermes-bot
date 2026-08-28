#!/usr/bin/env bash
# kalman_nostr_publisher.sh — Publish Kalman pricing state as kind-30315
# replaceable Nostr event for cross-VPS state sharing (contextvm data plane).
#
# Uses the `nak` CLI (available at ~/.local/bin/nak) for reliable relay publishing.
# Runs after exhaustion-gate.py in the 5-min cron.

set -euo pipefail

STATE_FILE="$HOME/.hermes/bot/kalman_pricing.json"
NSEC_FILE="$HOME/.hermes/bot/kalman_npub.nsec"
LOG="$HOME/.hermes/bot/nostr_publish.log"

RELAYS="wss://relay.primal.net wss://nostr.oxtr.dev wss://relay.damus.io"

if [[ ! -f "$STATE_FILE" ]] || [[ ! -f "$NSEC_FILE" ]]; then
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Missing state or nsec — skipping" >> "$LOG"
    exit 0
fi

NSEC=$(cat "$NSEC_FILE")

# Build compact content from kalman_pricing.json
CONTENT=$(python3 -c "
import json, os, sys
state = json.load(open(sys.argv[1]))
content = {
    'generated_at': state.get('generated_at'),
    'providers': {k: {
        'price': v.get('effective_rate_per_m'),
        'p_exhaust': v.get('p_exhaust'),
        'wk_pct': v.get('weekly_used_pct'),
        'sess_pct': v.get('session_used_pct'),
        'delisted': v.get('delisted', False),
    } for k, v in state.get('providers', {}).items()},
    'accuracy': state.get('accuracy'),
    'sat_per_usd': state.get('sat_per_usd'),
    'node': os.uname().nodename,
}
print(json.dumps(content, separators=(',', ':')))
" "$STATE_FILE")

# Publish via nak
RESULT=$(~/.local/bin/nak event \
    --kind 30315 \
    --tag "d:kalman-pricing-state" \
    --content "$CONTENT" \
    --sec "$NSEC" \
    $RELAYS 2>&1) || true

# Log result
TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
if echo "$RESULT" | grep -q "success"; then
    RELAY_COUNT=$(echo "$RESULT" | grep -c "success")
    echo "[$TS] Published to $RELAY_COUNT/$(echo $RELAYS | wc -w) relays (${#CONTENT} bytes)" >> "$LOG"
else
    echo "[$TS] Publish result: $RESULT" >> "$LOG"
fi
