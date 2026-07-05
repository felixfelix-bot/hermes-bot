#!/bin/bash
# kalman-dashboard-deploy.sh — Rebuild data.json + index.html, deploy to nsite
# Runs via cron every 10 min. Silent on success, outputs only on error.
# Uses build_v3.py for enhanced data (API key switching, anomaly details, log scale).

set -euo pipefail
export PATH="$HOME/.deno/bin:$HOME/.local/bin:$PATH"

NSITE_DIR="$HOME/nsites/kalman-data"
NSEC=$(cat "$HOME/.hermes/state/kalman-data-nsec.key" 2>/dev/null || echo "")

if [ -z "$NSEC" ]; then
    echo "ERROR: kalman-data-nsec.key is empty or missing"
    exit 1
fi

# Step 1: Regenerate data.json + index.html using build_v3.py
python3 "$NSITE_DIR/scripts/build_v3.py" 2>&1 || {
    echo "ERROR: build_v3.py failed"
    exit 1
}

# Step 2: Deploy to nsite (cd into dir, use . not absolute path)
cd "$NSITE_DIR"
timeout 90 nsyte deploy . \
    --sec "$NSEC" \
    --non-interactive \
    --skip-secrets-scan \
    --force 2>&1 | grep -E "SUCCESS|FAIL|error|ERROR|✓|❌|published" || true

exit 0
