#!/bin/bash
# nostr-kanban-inbound-sync.sh — Import Nostr kanbanstr cards from external
# maintainers (Endo/Arjen) → local human-gate board.
#
# Counterpart to nostr-kanban-sync.sh (outbound).
# Run as: hermes cron --no-agent --script scripts/nostr-kanban-inbound-sync.sh
# Silent when no new external cards. Creates local shadow tasks on import.
#
# Flow:
#   1. Query kind 30302 events filtered by a-tag = 30301:<pubkey>:<board-id>
#   2. Exclude our own pubkey (self-published outbound cards)
#   3. Diff against state file to find new external cards
#   4. INSERT shadow tasks into the human-gate kanban DB
#   5. Map Nostr s-tag → local task status

set -euo pipefail

source ~/nostr-glasses/secrets/.env 2>/dev/null || { echo "No Nostr key configured"; exit 0; }

BOARD_PUBKEY="e18a1d171a59d874edd336472afeb3a614d3dc83397dd097e922a99dcee02133"
BOARD_ID="net4sats-human-gate"
RELAYS="wss://relay.damus.io wss://nos.lol"
STATE_FILE=~/.hermes/state/nostr-kanban-inbound-sync.json
LOCAL_DB="/home/c03rad0r/.hermes/kanban/boards/human-gate/kanban.db"

# Ensure state directory exists
mkdir -p "$(dirname "$STATE_FILE")"

# Derive our own pubkey so we can exclude self-published cards
MY_PUBKEY=$(nak key public "$NOSTR_SECRET_KEY" 2>/dev/null || echo "")

# ── Query kind 30302 events on the board from relays ──────────────────
# nak req connects, sends the filter, prints matching events as JSONL, closes on EOSE.
EVENTS=$(nak req -k 30302 -t "a=30301:${BOARD_PUBKEY}:${BOARD_ID}" $RELAYS 2>/dev/null || echo "")

if [ -z "$EVENTS" ]; then
    exit 0  # Silent — no events on the board
fi

# ── Process: filter external, diff state, create shadow tasks ─────────
# Pipe events through Python for JSON parsing + SQLite insert.
RESULT=$(echo "$EVENTS" | MY_PUBKEY="$MY_PUBKEY" STATE_FILE="$STATE_FILE" LOCAL_DB="$LOCAL_DB" python3 -c '
import json, sys, sqlite3, time, os

events_raw = sys.stdin.read()
my_pubkey  = os.environ["MY_PUBKEY"]
state_file = os.environ["STATE_FILE"]
db_path    = os.environ["LOCAL_DB"]

# ── Parse events (nak outputs JSONL) ──
events = []
for line in events_raw.strip().split("\n"):
    line = line.strip()
    if not line:
        continue
    try:
        events.append(json.loads(line))
    except json.JSONDecodeError:
        pass

# ── Load previously imported event ids from state file ──
imported = set()
if os.path.exists(state_file):
    try:
        with open(state_file) as f:
            imported = set(json.load(f).get("imported_ids", []))
    except (json.JSONDecodeError, KeyError):
        pass

# ── Filter to external (non-self) events, dedupe by event id ──
new_cards = []
seen_ids  = set()
for ev in events:
    eid = ev.get("id", "")
    if not eid or eid in seen_ids:
        continue
    seen_ids.add(eid)

    # Skip our own outbound cards
    if ev.get("pubkey") == my_pubkey:
        continue
    # Skip already imported
    if eid in imported:
        continue

    # ── Extract tags ──
    tags       = ev.get("tags", [])
    title      = ""
    desc       = ""
    status_tag = ""
    card_d     = ""
    for tag in tags:
        if len(tag) >= 2:
            if   tag[0] == "title":       title      = tag[1]
            elif tag[0] == "description": desc       = tag[1]
            elif tag[0] == "s":           status_tag = tag[1]
            elif tag[0] == "d":           card_d     = tag[1]

    if not title:
        title = f"Nostr card: {card_d or eid[:12]}"

    # ── Map Nostr s-tag → local Hermes status ──
    status_map = {
        "todo":        "todo",
        "backlog":     "todo",
        "doing":       "running",
        "inprogress":  "running",
        "progress":    "running",
        "review":      "blocked",
        "humanreview": "blocked",
        "blocked":     "blocked",
        "done":        "done",
        "closed":      "done",
        "complete":    "done",
    }
    local_status = status_map.get(status_tag.lower(), "blocked")

    new_cards.append({
        "event_id":     eid,
        "pubkey":       ev.get("pubkey", ""),
        "title":        title,
        "description":  desc,
        "status_tag":   status_tag,
        "card_d":       card_d,
        "local_status": local_status,
        "created_at":   ev.get("created_at", int(time.time())),
    })

if not new_cards:
    print(json.dumps({"created": 0, "scanned": len(events)}))
    sys.exit(0)

# ── Create local shadow tasks in human-gate DB ──
created = []
conn = sqlite3.connect(db_path)
c = conn.cursor()

for card in new_cards:
    # Use a stable shadow id derived from the Nostr event id
    task_id = f"nostr-{card[\"event_id\"][:16]}"

    body = (
        f"Imported from Nostr kanbanstr (kind 30302)\n\n"
        f"Event ID: {card[\"event_id\"]}\n"
        f"Author pubkey: {card[\"pubkey\"]}\n"
        f"Card UUID (d-tag): {card[\"card_d\"]}\n"
        f"Nostr status: {card[\"status_tag\"]}\n\n"
        f"{card[\"description\"]}\n\n"
        f"---\n_Synced by nostr-kanban-inbound-sync.sh_"
    )

    try:
        c.execute(
            """INSERT INTO tasks
                 (id, title, body, assignee, status, priority,
                  created_by, created_at, workspace_kind)
               VALUES (?, ?, ?, 'human-gate', ?, 5,
                       'nostr-sync', ?, 'scratch')""",
            (task_id, card["title"], body, card["local_status"], card["created_at"]),
        )
        created.append(card["event_id"])
    except sqlite3.IntegrityError:
        pass  # Task id already exists — skip

conn.commit()
conn.close()

# ── Update state file ──
imported.update(created)
with open(state_file, "w") as f:
    json.dump({"imported_ids": sorted(imported)}, f, indent=2)

print(json.dumps({"created": len(created), "scanned": len(events)}))
' 2>&1)

# ── Report ──
CREATED=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('created',0))" 2>/dev/null || echo 0)

if [ "$CREATED" -gt 0 ]; then
    echo "📥 Imported $CREATED external Nostr card(s) into human-gate board"
fi
# Silent on zero new cards (cron-friendly)
