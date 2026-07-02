#!/bin/bash
# nostr-kanban-sync.sh — Sync Hermes human-gate board to Nostr (kanbanstr)
# Run as: hermes cron --no-agent --script scripts/nostr-kanban-sync.sh
# Silent when no pending items. Alerts on new items only.

set -euo pipefail

source ~/nostr-glasses/secrets/.env 2>/dev/null || { echo "No Nostr key configured"; exit 0; }

BOARD_PUBKEY="e18a1d171a59d874edd336472afeb3a614d3dc83397dd097e922a99dcee02133"
BOARD_ID="net4sats-human-gate"
RELAYS="wss://relay.damus.io wss://nos.lol"
STATE_FILE=~/.hermes/state/nostr-kanban-sync.json

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

if [ "$ITEM_COUNT" -eq 0 ]; then
    exit 0  # Silent — nothing to sync
fi

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

if [ -z "$NEW_ITEMS" ]; then
    exit 0  # Silent — no new items
fi

# Publish new items as kanbanstr cards (kind 30302)
PUBLISHED=0
echo "$NEW_ITEMS" | while IFS= read -r line; do
    [ -z "$line" ] && continue
    CARD_ID=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
    TITLE=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin)['title'])" 2>/dev/null)

    # Generate card UUID
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

# Report what was synced
echo "📋 Synced $ITEM_COUNT human-gate item(s) to Nostr kanbanstr board"
