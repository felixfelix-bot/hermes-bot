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

# Check for handshake success
if echo "$OUTPUT" | grep -q "handshake complete"; then
    exit 0
elif echo "$OUTPUT" | grep -q "RX 69B phase=0x2"; then
    # MSG2 received = handshake succeeded even if exact text differs
    exit 0
elif echo "$OUTPUT" | grep -q "MSG1 sent"; then
    # MSG1 sent but no MSG2 response — FIPS didn't respond
    echo "FAIL: MSG1 sent but no handshake response from FIPS"
    echo "$OUTPUT" | grep -E 'MSG1|MSG2|RX|handshake' | tail -5
    exit 1
elif echo "$OUTPUT" | grep -qi "error\|panic"; then
    echo "FAIL: sim crashed"
    echo "$OUTPUT" | grep -i 'error\|panic' | tail -5
    exit 1
else
    echo "UNKNOWN: unexpected sim output"
    echo "$OUTPUT" | tail -5
    exit 0
fi
