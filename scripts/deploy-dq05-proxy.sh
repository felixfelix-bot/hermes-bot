#!/usr/bin/env bash
# deploy-dq05-proxy.sh — P0-9 (2026-09-08): bring DQ05's zai-proxy to FULL
# flat-router parity with T470 so it can serve via the market router instead
# of degrading to a dumb key-rotation zombie.
#
# Fixes the documented DQ05 zombie: zai_proxy auto-discovers
# ~/merchant-routing-engine (src.* imports) and ~/.hermes/bot/flat_router.py;
# when either is missing the proxy silently loses Kalman/LiveRouter/dispatch-gate
# and serves blind quota (windows "unknown", used_pct 0).
#
# Usage:
#   bash scripts/deploy-dq05-proxy.sh [ssh-destination] [ssh-key]
# Defaults: destination c03rad0r@100.90.22.201 (netbird), key ~/.ssh/id_dq05.
#
# What it does (idempotent; safe to re-run):
#   1. Sync ~/merchant-routing-engine -> DQ05 (excl. .git/demo/datasets/logs).
#   2. Sync ~/.hermes/bot runtime files (top-level *.py/*.json, src/, scripts/,
#      *.key_disabled_* flags) -> DQ05, keeping DQ05-local *.db intact.
#   3. Merge T470 manager .env into DQ05's (T470 value wins; DQ05-only keys
#      preserved) — NO secrets leave the two hosts, nothing is printed.
#   4. Install zai-proxy.service.d drop-ins (feature flags + spend-cap) and
#      set ZAI_NODE_SOURCE so /kalman-pricing + 30315 telemetry attribute
#      to DQ05, not the hardcoded "T470".
#   5. Restart the user service, then verify: active, real quota windows,
#      correct kalman-pricing source, and a live dispatch through :9099.
#
# Requires on DQ05: ~/.hermes/hermes-agent/venv with numpy + websocket-client
#   (install once: pip install numpy websocket-client requests).
set -euo pipefail

SRC_BOT="$HOME/.hermes/bot"
SRC_MRE="$HOME/merchant-routing-engine"
DST="${1:-c03rad0r@100.90.22.201}"   # netbird IP of DQ05 (c03rad0r-DQ05proplus)
KEY="${2:-$HOME/.ssh/id_dq05}"
SSH="ssh -o ConnectTimeout=10 -i $KEY $DST"

[ -d "$SRC_BOT" ] || { echo "FATAL: $SRC_BOT missing (run from T470)"; exit 1; }
[ -d "$SRC_MRE" ] || { echo "FATAL: $SRC_MRE missing"; exit 1; }

echo "==> [1/6] sync merchant-routing-engine"
tar -C "$HOME" -czf - \
  --exclude='merchant-routing-engine/.git' \
  --exclude='merchant-routing-engine/__pycache__' \
  --exclude='merchant-routing-engine/demo' \
  --exclude='merchant-routing-engine/datasets' \
  --exclude='merchant-routing-engine/logs' \
  --exclude='merchant-routing-engine/htmlcov' \
  merchant-routing-engine \
| $SSH 'tar -C "$HOME" -xzf - && echo MRE-OK'

echo "==> [2/6] sync bot runtime files (keeps DQ05 *.db)"
cd "$SRC_BOT"
tar -czf - \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' --exclude='*.db-journal' \
  --exclude='*.bak*' --exclude='*.log' --exclude='.enable_live_routing' \
  --exclude='graphify-out' --exclude='backups' \
  $(ls *.py *.json 2>/dev/null | tr '\n' ' ') src scripts \
| $SSH 'cd "$HOME/.hermes/bot" && tar -xzf - && echo BOT-OK'

echo "==> [3/6] sync .key_disabled_* exclusion flags"
cd "$SRC_BOT"
FLAGS=$(ls .key_disabled_* 2>/dev/null || true)
if [ -n "$FLAGS" ]; then
  tar -czf - $FLAGS | $SSH 'cd "$HOME/.hermes/bot" && tar -xzf - && echo FLAGS-OK'
else
  echo "no exclusion flags on source; skipping"
fi

echo "==> [4/6] merge manager .env (T470 value wins, DQ05-only preserved)"
$SSH 'cat > env_t470_src.txt' < "$HOME/.hermes/profiles/manager/.env"
$SSH 'cat > env_merge.py' <<'PYEOF'
import sys
def read_env(path):
    keys, order = {}, []
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name = name.strip()
        if name and name not in keys:
            keys[name] = val; order.append(name)
    return keys, order
t, to = read_env("env_t470_src.txt")
d, do = read_env(sys.argv[1])
out = [f"{n}={t[n]}" for n in to] + [f"{n}={d[n]}" for n in do if n not in t]
open("env_merged.txt", "w", encoding="utf-8").write("\n".join(out) + "\n")
print(f"merged {len(out)} keys (T470 {len(to)}, DQ05-only preserved {len([n for n in do if n not in t])})")
PYEOF
$SSH 'ENV="$HOME/.hermes/profiles/manager/.env"; TS=$(date -u +%Y%m%dT%H%M%SZ); cp -p "$ENV" "$ENV.bak-dq05deploy-$TS"; python3 env_merge.py "$ENV"; chmod 600 env_merged.txt; mv env_merged.txt "$ENV"; rm -f env_t470_src.txt env_merge.py; echo ENV-OK'

echo "==> [5/6] install systemd drop-ins + restart"
$SSH 'cat > finalize.sh' <<'EOF'
#!/bin/bash
set -euo pipefail
UNIT_D="$HOME/.config/systemd/user/zai-proxy.service.d"
mkdir -p "$UNIT_D"
cat > "$UNIT_D/env-features.conf" <<'E2'
[Service]
# P0-9: feature flags mirrored from T470 zai-proxy unit so the DQ05
# flat-router proxy runs the same pricing/pressure machinery.
Environment=ZAI_QUOTA_PRESSURE_ENABLED=true
Environment=OLLAMA_QUOTA_PRESSURE_ENABLED=true
Environment=OLLAMA_EXTRA_USAGE_ENABLED=true
Environment=LIVE_ROUTER_DYNAMIC_RATES_ENABLED=true
Environment=PPQ_QUOTA_PRESSURE_ENABLED=true
Environment=OPENROUTER_CREDIT_PRESSURE_ENABLED=true
Environment=DEEPINFRA_CREDIT_PRESSURE_ENABLED=true
Environment=DEEPINFRA_QUOTA_PRESSURE_ENABLED=true
Environment=OPENROUTER_QUOTA_PRESSURE_ENABLED=true
Environment=ROUTSTR_QUOTA_PRESSURE_ENABLED=true
Environment=ROUTSTRD_QUOTA_PRESSURE_ENABLED=true
Environment=PER_MODEL_PRICING_ENABLED=true
# P0-9: telemetry source identity (zai_proxy /kalman-pricing + 30315 publisher)
Environment=ZAI_NODE_SOURCE=c03rad0r-DQ05proplus
E2
cat > "$UNIT_D/spend-cap.conf" <<'E3'
[Service]
# D6 (2026-09-02) burn-reduction: real caps again.
Environment=SPEND_CAP_MANAGER=999999
Environment=SPEND_CAP_WORKER=999999
Environment=SPEND_CAP_METERED=25
E3
systemctl --user daemon-reload
systemctl --user restart zai-proxy
sleep 6
echo "active: $(systemctl --user is-active zai-proxy)"
EOF
$SSH 'bash finalize.sh; rm -f finalize.sh'

echo "==> [6/6] verification"
$SSH 'cd "$HOME/.hermes/bot" && timeout 45 "$HOME/.hermes/hermes-agent/venv/bin/python" -c "import zai_proxy; print(\"IMPORT_OK\")" 2>&1 | grep -E "IMPORT_OK|DISABLED" | head -5; echo "--- /quota windows ---"; curl -s -m 8 http://127.0.0.1:9099/quota | grep -E "\"name\"|\"used_pct\"|\"locked\"" | head -8; echo "--- /kalman-pricing source ---"; curl -s -m 8 http://127.0.0.1:9099/kalman-pricing | head -c 200; echo; echo "--- live dispatch ---"; curl -s -m 90 -o /dev/null -w "HTTP %{http_code}\n" -X POST http://127.0.0.1:9099/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply OK\"}],\"max_tokens\":8}"'

echo "DEPLOY COMPLETE"
