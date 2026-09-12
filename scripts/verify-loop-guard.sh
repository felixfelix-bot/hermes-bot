#!/usr/bin/env bash
# verify-loop-guard.sh — live integration test for the zai_proxy routing-loop guard.
#
# Background: routstrd (:8008) is an upstream provider of zai_proxy, while
# routstrd's config.json lists zai_proxy (localhost:9099) in staticProviders and
# passthroughs directly back to it. A single buyer request could therefore
# ping-pong buyer → routstrd → proxy → routstrd → proxy …  Each hop was logged
# as its own api_calls row (35 identical calls in 0.7 s; 39% of one hour's calls
# were duplicates; est. $11/hr).
#
# The guard (zai_proxy.py, 2026-09-12):
#   * reads/re-uses the X-Zai-Router-Hop header, stamping hop+1 on every
#     outbound provider call;
#   * never routes a re-entrant request (hop >= 1) back to the routstrd upstream;
#   * hard-rejects hop >= ZAI_MAX_HOPS (default 2) with 429 routing_loop.
#
# Usage: bash verify-loop-guard.sh [proxy_base]   (default http://localhost:9099)
# Exit:  0 = guard behaving correctly, 1 = regression.
set -uo pipefail
BASE="${1:-http://localhost:9099}"
BODY='{"model":"glm-4.5-flash","messages":[{"role":"user","content":"probe"}],"max_tokens":5}'
fail=0

req() { curl -s -m 30 -o /tmp/loopguard_body.txt -w '%{http_code}' \
        -X POST "$BASE/v1/chat/completions" -H 'Content-Type: application/json' "$@" -d "$BODY"; }

code_reentrant=$(req -H 'X-Zai-Router-Hop: 2')
if [ "$code_reentrant" = "429" ] && grep -q 'routing_loop' /tmp/loopguard_body.txt; then
  echo "PASS re-entrant hop=2 refused (429 routing_loop)"
else
  echo "FAIL re-entrant hop=2 expected 429/routing_loop, got $code_reentrant: $(head -c 160 /tmp/loopguard_body.txt)"
  fail=1
fi

code_first=$(req -H 'X-Zai-Router-Hop: 1')
if [ "$code_first" = "200" ]; then
  echo "PASS first bounce hop=1 still served (200)"
else
  echo "FAIL hop=1 expected 200, got $code_first"
  fail=1
fi

code_normal=$(req)
if [ "$code_normal" = "200" ]; then
  echo "PASS normal request unaffected (200)"
else
  echo "FAIL normal request expected 200, got $code_normal"
  fail=1
fi

exit "$fail"
