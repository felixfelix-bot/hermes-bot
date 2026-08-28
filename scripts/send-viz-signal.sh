#!/usr/bin/env bash
# send-viz-signal.sh — Send viz plots to hermes-admin-setup via signal-cli JSON-RPC.
#
# Usage:
#   send-viz-signal.sh [--digest] [--plot NAME] [--message "text"]
#
# Modes:
#   (default)   Send all 4 plots + ASCII summary table
#   --digest    Send envelope + quota-heatmap + ASCII (daily digest)
#   --plot NAME Send a single named plot (price-envelope|price-heatmap|quota-heatmap|surface-ollama_cloud)
#
# signal-cli daemon v0.14.5 on 127.0.0.1:8080 supports attachments as local file paths.

set -euo pipefail
RPC_URL="http://localhost:8080/api/v1/rpc"
ACCOUNT="+18102940908"
GROUP_ID="V8tnIinI5Yh6wAqXj2vGa0PfJ27j6zHLgpeZJexODEA="
VIZ_DIR="$HOME/.hermes/viz"
LOG="$HOME/.hermes/viz/send.log"

# Build JSON-safe attachment array via python
build_payload() {
    local message="$1"; shift
    local attachments=("$@")
    python3 -c "
import json, sys
msg = sys.argv[1]
atts = sys.argv[2:]
print(json.dumps({
    'jsonrpc': '2.0',
    'method': 'send',
    'params': {
        'account': '$ACCOUNT',
        'groupId': '$GROUP_ID',
        'message': msg,
        'attachments': atts,
    },
    'id': 1,
}))
" "$message" "${attachments[@]}"
}

send() {
    local payload="$1"
    local resp
    resp=$(curl -s --max-time 30 -X POST "$RPC_URL" -H 'Content-Type: application/json' -d "$payload" 2>&1) || {
        echo "ERROR: curl failed: $resp" | tee -a "$LOG"
        return 1
    }
    # Check for error in response
    if echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if 'error' not in d else 1)" 2>/dev/null; then
        local ts; ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
        local sent_ts; sent_ts=$(echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('result',{}).get('timestamp','?'))" 2>/dev/null)
        echo "[$ts] OK sent ts=$sent_ts" | tee -a "$LOG"
    else
        echo "ERROR: signal-cli returned: $resp" | tee -a "$LOG"
        return 1
    fi
}

ASCII_FILE="$VIZ_DIR/ascii-summary.txt"
DEFAULT_PLOTS=(
    "$VIZ_DIR/price-envelope.png"
    "$VIZ_DIR/price-heatmap.png"
    "$VIZ_DIR/quota-heatmap.png"
    "$VIZ_DIR/surface-ollama_cloud.png"
    "$VIZ_DIR/headroom-weekly.png"
)
DIGEST_PLOTS=(
    "$VIZ_DIR/price-envelope.png"
    "$VIZ_DIR/quota-heatmap.png"
)

# args parse
MODE="all"
CUSTOM_MSG=""
PLOT_NAME=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --digest)  MODE="digest";  shift ;;
        --plot)    MODE="single"; PLOT_NAME="$2"; shift 2 ;;
        --message) CUSTOM_MSG="$2"; shift 2 ;;
        *)         shift ;;
    esac
done

# Trigger fresh render before sending (so plots reflect latest data)
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/bot/price_viz.py" >> "$LOG" 2>&1 || true

# Read ASCII summary
ASCII=""
if [[ -f "$ASCII_FILE" ]]; then
    ASCII=$(cat "$ASCII_FILE")
fi

case "$MODE" in
    all)
        MSG="${CUSTOM_MSG:-📊 Price Landscape Snapshot — $(date '+%H:%M %b %d %Z')}\n\n${ASCII}"
        PAYLOAD=$(build_payload "$MSG" "${DEFAULT_PLOTS[@]}")
        ;;
    digest)
        MSG="${CUSTOM_MSG:-📈 Daily Digest — $(date '+%Y-%m-%d')}\n\n${ASCII}"
        PAYLOAD=$(build_payload "$MSG" "${DIGEST_PLOTS[@]}")
        ;;
    single)
        PLOT_PATH="$VIZ_DIR/${PLOT_NAME}.png"
        if [[ ! -f "$PLOT_PATH" ]]; then
            echo "ERROR: plot not found: $PLOT_PATH" | tee -a "$LOG"
            exit 1
        fi
        MSG="${CUSTOM_MSG:-📊 Plot: ${PLOT_NAME} — $(date '+%H:%M %b %d %Z')}"
        PAYLOAD=$(build_payload "$MSG" "$PLOT_PATH")
        ;;
esac

PLOT_COUNT=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(len(d['params']['attachments']))" "$PAYLOAD" 2>/dev/null || echo "?")
echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Sending mode=$MODE plots=$PLOT_COUNT" | tee -a "$LOG"
send "$PAYLOAD"
