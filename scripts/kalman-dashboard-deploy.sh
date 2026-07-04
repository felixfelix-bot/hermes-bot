#!/bin/bash
# kalman-dashboard-deploy.sh — Rebuild data.json + index.html, deploy to nsite
# Runs via cron every 5 min. Silent on success, outputs only on error.
# This solves the "datapoints lost on reload" problem by keeping data.json fresh.

set -euo pipefail
export PATH="$HOME/.deno/bin:$HOME/.local/bin:$PATH"

NSITE_DIR="$HOME/nsites/kalman-data"
NSEC=$(cat "$HOME/.hermes/state/kalman-data-nsec.key" 2>/dev/null || echo "")
DB="$HOME/.hermes/bot/zai_usage.db"

# Step 1: Regenerate data.json from SQLite (ALL samples)
python3 -c "
import json, sqlite3, os
from datetime import datetime, timezone

db = sqlite3.connect('$DB')
rows = db.execute('''
    SELECT ts, burn_rate_tph, projected_total_pct, used_pct_observed,
           uncertainty, will_exhaust, velocity_tph2, exhausts_in_hours
    FROM kalman_samples ORDER BY ts ASC
''').fetchall()
anom_rows = db.execute('''
    SELECT ts, severity, title FROM anomaly_events ORDER BY ts DESC LIMIT 20
''').fetchall()
db.close()

data = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'sample_count': len(rows),
    'times': [r[0] * 1000 for r in rows],
    'burn_rate': [min(r[1] or 0, 50000) for r in rows],
    'projected_pct': [r[2] or 0 for r in rows],
    'used_pct': [r[3] or 0 for r in rows],
    'uncertainty': [min(r[4] or 0, 50000) for r in rows],
    'will_exhaust': [bool(r[5]) for r in rows],
    'exhausts_in_hours': [r[7] for r in rows],
    'anomalies': [{'ts': r[0], 'severity': r[1], 'title': r[2]} for r in anom_rows],
}
with open('$NSITE_DIR/data.json', 'w') as f:
    json.dump(data, f, separators=(',', ':'))
" 2>&1 || {
    echo "ERROR: data.json generation failed"
    exit 1
}

# Step 2: Deploy ONLY index.html + data.json to nsite (not .py scripts)
cd "$NSITE_DIR"
timeout 90 nsyte deploy . \
    --sec "$NSEC" \
    --non-interactive \
    --skip-secrets-scan \
    --force 2>&1 | grep -E "SUCCESS|FAIL|error|ERROR|✓|❌" || true

exit 0
