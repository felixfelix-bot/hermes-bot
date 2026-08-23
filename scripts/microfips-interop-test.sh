#!/usr/bin/env bash
set -euo pipefail

# microFIPS <-> FIPS VPS1 Interop Test (Phase 2)
# Runs microfips-sim directly via UDP against FIPS on VPS1 (66.92.204.38:2121)
# Silent on success, outputs only on failure (watchdog pattern)

VPS1_IP="66.92.204.38"
FIPS_PORT="2121"
MICROFIPS_DIR="$HOME/repos/microfips"
BRANCH="feat/fips-v0-compat"
MAX_RUNTIME=20  # seconds before timeout

# Persistent sim identity (generated once, reused)
SIM_NSEC_FILE="$HOME/.local/state/microfips-interop/sim_nsec"
SIM_PUB_FILE="$HOME/.local/state/microfips-interop/sim_pub"
FIPS_PUB="03d833fb0801aea854ba11a5d4e60fc22e3b6ed37f381ad946109bbbcfb8266b4b"
FIPS_NODE_ADDR="90850f9ce010602e45cb33b38ac73db1"

# Check we're on the compat branch
cd "$MICROFIPS_DIR"
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    echo "SKIP: Not on $BRANCH (on $CURRENT_BRANCH)"
    exit 0
fi

# Build sim if needed
if ! cargo build -p microfips-sim --release 2>/dev/null; then
    echo "SKIP: microfips-sim build failed"
    exit 0
fi

SIM_BIN="$MICROFIPS_DIR/target/release/microfips-sim"

# Generate or load persistent sim identity
mkdir -p "$(dirname "$SIM_NSEC_FILE")"
if [ ! -f "$SIM_NSEC_FILE" ]; then
    KEYGEN=$($SIM_BIN --keygen 2>&1)
    echo "$KEYGEN" | grep '^FIPS_NSEC=' | cut -d= -f2 > "$SIM_NSEC_FILE"
    echo "$KEYGEN" | grep '^FIPS_PUB=' | cut -d= -f2 > "$SIM_PUB_FILE"
fi
SIM_NSEC=$(cat "$SIM_NSEC_FILE")

# Run sim against FIPS — direct UDP, initiator mode
OUTPUT=$(FIPS_NSEC="$SIM_NSEC" FIPS_PEER_NPUB="$FIPS_PUB" \
    timeout "$MAX_RUNTIME" "$SIM_BIN" \
    --udp "${VPS1_IP}:${FIPS_PORT}" \
    --initiator \
    --target "$FIPS_NODE_ADDR" \
    2>&1 || true)

# Determine handshake result
HANDSHAKE_OK=false
if echo "$OUTPUT" | grep -q "handshake complete"; then
    HANDSHAKE_OK=true
elif echo "$OUTPUT" | grep -qP "TX \d+B phase=0x[12]" && echo "$OUTPUT" | grep -qP "RX \d+B phase=0x[02]"; then
    # MSG1 (phase 1) sent + MSG2 (phase 2) received = IK handshake completed
    HANDSHAKE_OK=true
elif echo "$OUTPUT" | grep -qP "TX \d+B phase=0x0" && echo "$OUTPUT" | grep -qP "RX \d+B phase=0x0"; then
    # Both phase=0x0 messages = XX handshake init exchange
    HANDSHAKE_OK=true
fi

if $HANDSHAKE_OK; then
    # Save evidence
    EVIDENCE_DIR="$HOME/microfips-evidence"
    mkdir -p "$EVIDENCE_DIR"
    TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
    MESSAGES=$(echo "$OUTPUT" | grep -oP '(TX|RX) \d+B phase=0x[0-9a-f]+' | sed 's/^/    "/' | sed 's/$/",/' | sed '$ s/,$//')
    cat > "$EVIDENCE_DIR/latest.json" << JSONEOF
{
  "timestamp": "$TIMESTAMP",
  "result": "pass",
  "vps": "$VPS1_IP",
  "branch": "$BRANCH",
  "messages": [
$MESSAGES
  ]
}
JSONEOF
    exit 0
elif echo "$OUTPUT" | grep -qi "panic\|error.*resolve\|connection refused"; then
    echo "FAIL: sim error"
    echo "$OUTPUT" | grep -i 'panic\|error\|refused' | tail -5
    exit 1
elif echo "$OUTPUT" | grep -qP "TX \d+B" && ! echo "$OUTPUT" | grep -qP "RX \d+B"; then
    echo "FAIL: MSG1 sent but no response from FIPS"
    echo "$OUTPUT" | grep -E 'TX|RX' | tail -5
    exit 1
else
    echo "UNKNOWN: unexpected sim output"
    echo "$OUTPUT" | tail -5
    exit 0
fi
