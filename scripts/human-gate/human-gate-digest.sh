#!/bin/bash
# human-gate-digest.sh — Deliver a digest of pending human-gate items
# Run as: hermes cron --no-agent --script scripts/human-gate-digest.sh
# Empty stdout = SILENT (nothing pending). Non-empty = alert delivered.

set -euo pipefail

# Count pending items across ALL boards that have blocked tasks
# with "human-gate" in the reason
PENDING=$(python3 -c "
import sqlite3, json, os, glob

boards_dir = os.path.expanduser('~/.hermes/kanban/boards')
items = []

for db_path in glob.glob(os.path.join(boards_dir, '*/kanban.db')):
    board_slug = os.path.basename(os.path.dirname(db_path))
    if board_slug == 'human-gate':
        continue  # skip the shadow board itself
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('SELECT id, title, status FROM tasks WHERE status = \"blocked\"')
        for tid, title, status in c.fetchall():
            # Check if it's a human-gate block
            c2 = conn.cursor()
            c2.execute('SELECT payload FROM task_events WHERE task_id = ? AND kind = \"blocked\" ORDER BY created_at DESC LIMIT 1', (tid,))
            row = c2.fetchone()
            if row and row[0] and 'human-gate' in str(row[0]).lower():
                items.append({'board': board_slug, 'task': tid, 'title': title})
        conn.close()
    except:
        pass

if items:
    print(f'📋 {len(items)} human-gate item(s) pending:')
    for i in items:
        print(f'  [{i[\"board\"]}] {i[\"title\"]} ({i[\"task\"]})')
" 2>/dev/null || true)

if [ -n "$PENDING" ]; then
    echo "$PENDING"
fi
