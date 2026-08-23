#!/bin/bash
# nostr-kanban-sync.sh — Sync Hermes kanban state to Nostr
#
# Two functions:
#   1. Human-gate board → Nostr kanbanstr (kind 30302) for external collaborators
#   2. ALL boards → Nostr bidirectional replication (kind 38010) for machine sync
#
# Run as: hermes cron --no-agent --script scripts/nostr-kanban-sync.sh
# Silent when no pending items. Alerts on new items only.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source ~/nostr-glasses/secrets/.env 2>/dev/null || { echo "No Nostr key configured"; exit 0; }

BOARD_PUBKEY="e18a1d171a59d874edd336472afeb3a614d3dc83397dd097e922a99dcee02133"
BOARD_ID="net4sats-human-gate"
RELAYS="wss://relay.damus.io wss://nos.lol"
STATE_FILE=~/.hermes/state/nostr-kanban-sync.json

# ════════════════════════════════════════════════════════════════════════════
# PART 1: Human-gate kanbanstr publishing (kind 30302) — external collaborators
# ════════════════════════════════════════════════════════════════════════════

# Read pending human-gate items from the local Hermes board
PENDING=$(python3 -c "
import sqlite3, json, sys
try:
    conn = sqlite3.connect('/home/c03rad0r/.hermes/kanban/boards/human-gate/kanban.db')
    c = conn.cursor()
    c.execute('SELECT id, title, status FROM tasks WHERE status IN (\"ready\", \"running\") ORDER BY created_at')
    items = [{'id': r[0], 'title': r[1], 'status': r[2]} for r in c.fetchall()]
    conn.close()
    print(json.dumps(items))
except:
    print('[]')
" 2>/dev/null || echo '[]')

ITEM_COUNT=$(echo "$PENDING" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)

if [ "$ITEM_COUNT" -gt 0 ]; then
    # Load previously synced items to detect changes
    PREV_SYNCED=""
    if [ -f "$STATE_FILE" ]; then
        PREV_SYNCED=$(cat "$STATE_FILE")
    fi

    # Find new items not yet published to Nostr
    NEW_ITEMS=$(python3 -c "
import json, sys
pending = json.loads('''$PENDING''')
prev = json.loads('''$PREV_SYNCED''') if '''$PREV_SYNCED''' else []
prev_ids = {p.get('id') for p in prev}
new = [p for p in pending if p['id'] not in prev_ids]
for item in new:
    print(json.dumps(item))
" 2>/dev/null || true)

    if [ -n "$NEW_ITEMS" ]; then
        # Publish new items as kanbanstr cards (kind 30302)
        echo "$NEW_ITEMS" | while IFS= read -r line; do
            [ -z "$line" ] && continue
            CARD_ID=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
            TITLE=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin)['title'])" 2>/dev/null)

            CARD_UUID=$(python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null)

            nak event \
                --sec "$NOSTR_SECRET_KEY" \
                -k 30302 \
                -d "$CARD_UUID" \
                -t "title=$TITLE" \
                -t "description=Source: human-gate/$CARD_ID" \
                -t "a=30301:$BOARD_PUBKEY:$BOARD_ID" \
                -t "s=humanreview" \
                -t "rank=1" \
                $RELAYS 2>/dev/null && echo "Published: $CARD_ID → $CARD_UUID" || echo "Failed: $CARD_ID"
        done

        # Save state
        echo "$PENDING" > "$STATE_FILE"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# PART 2: All-board bidirectional replication (kind 38010) — machine sync
# ════════════════════════════════════════════════════════════════════════════
# Publishes local kanban changes (all boards) to Nostr for the peer machine.
# Counterpart: nostr-kanban-inbound-sync.sh subscribes and applies changes.

timeout 120 python3 "$SCRIPT_DIR/kanban-nostr-replicate.py" --outbound --timeout 15 2>/dev/null || true
