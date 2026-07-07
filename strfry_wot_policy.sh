#!/usr/bin/env bash
# strfry write-policy — queries relatr ContextVM for each inbound event.
# strfry calls this with JSON on stdin. We check the pubkey's trust score.
# Returns: {"action": "accept"} or {"action": "reject"}
set -u
input=$(cat)
pubkey=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('event',{}).get('pubkey',''))" 2>/dev/null)

if [ -z "$pubkey" ]; then
    echo '{"action":"reject","msg":"no pubkey"}'
    exit 0
fi

result=$(curl -s --max-time 2 -X POST http://127.0.0.1:7778/check \
    -H "Content-Type: application/json" \
    -d "{\"pubkey\":\"$pubkey\"}" 2>/dev/null)

allowed=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('allowed',False))" 2>/dev/null)

if [ "$allowed" = "True" ]; then
    echo '{"action":"accept"}'
else
    echo '{"action":"reject","msg":"pubkey not in web of trust"}'
fi
