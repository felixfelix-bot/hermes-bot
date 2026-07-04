#!/bin/bash
# worker-dashboard-deploy.sh — Regenerate dashboard, deploy to nsite + ngit
# Runs via cron every 30 min. Silent on success.

set -euo pipefail
export PATH="$HOME/.deno/bin:$HOME/.local/bin:$PATH"

REPORTS_DIR="$HOME/reports"
NSITE_DIR="$HOME/nsites/worker-dashboard"
DASHBOARD_NSEC=$(cat "$HOME/.hermes/state/worker-dashboard-nsec.key" 2>/dev/null || echo "")
MAIN_NSEC="nsec1gszj7vzu56wjxk0kaja4tc3n8p4xachjev7maev4w82a8xd69ddsm7t63e"
MAIN_NPUB="npub12m5exm2uk3xa674cc5r0hlyvccs5xxn7qv83ezuteefv5972nquq4j4szl"

# Step 1: Regenerate the dashboard
python3 "$HOME/.hermes/profiles/manager/scripts/worker_metrics_plot.py" --out "$REPORTS_DIR/worker-dashboard.html" 2>&1 || {
    echo "ERROR: Dashboard plot failed"
    exit 1
}

# Step 2: Copy to nsite deploy dir
cp "$REPORTS_DIR/worker-dashboard.html" "$NSITE_DIR/index.html"

# Step 3: Deploy to nsite (dedicated npub for the dashboard)
if [ -n "$DASHBOARD_NSEC" ]; then
    cd "$NSITE_DIR"
    nsyte deploy . \
        --sec "$DASHBOARD_NSEC" \
        --non-interactive \
        --skip-secrets-scan \
        --force 2>&1 | grep -E "SUCCESS|FAIL|error|ERROR" || true
fi

# Step 4: Commit + push to ngit (main npub)
cd "$NSITE_DIR"
git add index.html 2>/dev/null || true
git diff --cached --quiet && exit 0  # nothing to commit
git commit -m "auto-update: worker dashboard $(date -u +%Y-%m-%dT%H:%MZ)" 2>/dev/null || true
git push origin master --no-verify 2>&1 | grep -v "^\s*$" | tail -3 || true

exit 0
