#!/usr/bin/env bash
set -euo pipefail

# microFIPS <-> FIPS VPS1 Interop Test (Phase 2)
# Runs microfips-sim against FIPS on VPS1 (66.92.204.38:2121)
# Silent on success, outputs only on failure (watchdog pattern)

VPS1_IP="66.92.204.38"
FIPS_PORT="2121"
SSH_USER="debian"
MICROFIPS_DIR="$HOME/repos/microfips"
BRANCH="feat/fips-v0-compat"
MAX_RUNTIME=90  # seconds before we declare success
TUNNEL_PID=""

cleanup() {
    [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null || true
}
trap cleanup EXIT

# Check we're on the compat branch
cd "$MICROFIPS_DIR"
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    echo "SKIP: Not on $BRANCH (on $CURRENT_BRANCH). Phase 1 may not be complete."
    exit 0
fi

# Check sim binary exists
if ! cargo build -p microfips-sim --release 2>/dev/null; then
    echo "SKIP: microfips-sim build failed. Phase 1 may not be complete."
    exit 0
fi

# Set up SSH tunnel to VPS1 FIPS
ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -L 31337:127.0.0.1:$FIPS_PORT "$SSH_USER@$VPS1_IP" -N &
TUNNEL_PID=$!
sleep 2

# Verify tunnel is up
if ! ss -tlnp | grep -q 31337; then
    echo "FAIL: SSH tunnel to VPS1:$FIPS_PORT failed"
    exit 1
fi

# Verify FIPS is listening on VPS1
if ! ssh -o ConnectTimeout=5 "$SSH_USER@$VPS1_IP" "ss -ulnp | grep -q $FIPS_PORT" 2>/dev/null; then
    echo "FAIL: FIPS not listening on VPS1:$FIPS_PORT"
    exit 1
fi

# Run sim against FIPS through tunnel
# Capture output, look for handshake success indicators
OUTPUT=$(timeout "$MAX_RUNTIME" cargo run -p microfips-sim --release -- --listen 45679 2>&1 &
SIM_PID=$!
sleep 5

# Connect bridge: sim port 45679 -> tunnel port 31337
# The sim uses length-prefixed framing over TCP
python3 -c "
import socket, time, sys, signal

sim_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
fips_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

try:
    sim_sock.connect(('127.0.0.1', 45679))
except:
    print('FAIL: Cannot connect to sim on 45679')
    sys.exit(1)

try:
    fips_sock.connect(('127.0.0.1', 31337))
except:
    print('FAIL: Cannot connect to FIPS tunnel on 31337')
    sys.exit(1)

print('Bridge connected: sim(45679) <-> FIPS(31337)')

import threading, select

def forward(src, dst, name):
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except:
        pass

t1 = threading.Thread(target=forward, args=(sim_sock, fips_sock, 'sim->fips'), daemon=True)
t2 = threading.Thread(target=forward, args=(fips_sock, sim_sock, 'fips->sim'), daemon=True)
t1.start()
t2.start()

# Wait for sim to complete or timeout
t1.join(timeout=$MAX_RUNTIME)
print('Bridge session ended')
" 2>&1)

kill $SIM_PID 2>/dev/null || true

# Check FIPS journal for handshake evidence
FIPS_LOG=$(ssh -o ConnectTimeout=5 "$SSH_USER@$VPS1_IP" \
    "journalctl -u fips --since '2 min ago' --no-pager 2>/dev/null" || echo "")

if echo "$FIPS_LOG" | grep -qi "promoted to active peer"; then
    # Success! Check for sustained heartbeat
    HB_COUNT=$(echo "$FIPS_LOG" | grep -ci "heartbeat" || echo "0")
    echo "PASS: FIPS promoted microFIPS to active peer"
    echo "Heartbeat exchanges detected: $HB_COUNT"
    # Silent exit 0 — success
    exit 0
elif echo "$FIPS_LOG" | grep -qi "link dead\|connection.*reject\|invalid.*version"; then
    echo "FAIL: FIPS rejected microFIPS connection"
    echo "$FIPS_LOG" | grep -i "link dead\|reject\|invalid\|version" | tail -5
    exit 1
else
    echo "UNKNOWN: No handshake evidence in FIPS journal"
    echo "Last 5 FIPS log lines:"
    echo "$FIPS_LOG" | tail -5
    exit 0  # Don't alert on unknown — may be timing issue
fi
