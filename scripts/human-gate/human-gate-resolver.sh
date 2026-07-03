#!/bin/bash
# human-gate-resolver.sh — Scan completed human-gate shadows, unblock originals
# Run as: hermes cron --no-agent --script scripts/human-gate-resolver.sh
# Silent when nothing to resolve. Reports resolved items only.

set -euo pipefail

# Find completed shadow tasks and unblock their parent tasks
RESULTS=$(python3 -c "
import sqlite3, json, subprocess, sys

resolved = []
try:
    conn = sqlite3.connect('/home/c03rad0r/.hermes/kanban/boards/human-gate/kanban.db')
    c = conn.cursor()
    # Find done/archived tasks that haven't been resolved yet
    c.execute('SELECT id, title, body FROM tasks WHERE status = \"done\"')
    for tid, title, body in c.fetchall():
        # Check body for source board/task info (either format)
        body_lower = (body or '').lower()
        has_source = 'source_board' in body_lower or 'source board' in body_lower
        if body and has_source:
            import re
            # Match both formats:
            #   \"Source board: fips\"  (human, space, Title case)
            #   \"source_board: fips\"  (JSON, underscore)
            #   \"source_board\":\"fips\" (escaped JSON)
            board_match = re.search(r'[Ss]ource[_\s][Bb]oard[\":\s]+([\w-]+)', body)
            task_match = re.search(r'[Ss]ource[_\s][Tt]ask[\":\s]+([\w-]+)', body)
            if board_match and task_match:
                src_board = board_match.group(1)
                src_task = task_match.group(1)
                # Try to unblock the original task
                r = subprocess.run(
                    ['hermes', 'kanban', '--board', src_board, 'unblock', src_task],
                    capture_output=True, text=True, timeout=10
                )
                if r.returncode == 0:
                    resolved.append(f'{src_board}/{src_task} ← human-gate/{tid}')
                # Archive the shadow task
                subprocess.run(
                    ['hermes', 'kanban', '--board', 'human-gate', 'archive', tid],
                    capture_output=True, text=True, timeout=10
                )
    conn.close()
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)

if resolved:
    for r in resolved:
        print(f'✅ Resolved: {r}')
" 2>/dev/null || true)

if [ -n "$RESULTS" ]; then
    echo "$RESULTS"
fi
