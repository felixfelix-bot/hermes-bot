#!/usr/bin/env python3
"""Send diff to kimi-k3:cloud via zai proxy for cold review (Gate 2.5)."""
import json
import urllib.request
import sys
from pathlib import Path

diff = Path("/tmp/kimi_review_diff.txt").read_text()

prompt = f"""You are reviewing a cold diff for quality gate G2.5. DO NOT modify any files.
This is a 1-D Kalman filter that tracks context growth rate (tokens/call) from zai_usage.db
and adjusts compression.threshold via `hermes config set`. context_length is read dynamically
from config.yaml (NOT hardcoded). K_SENSITIVITY=0.00004, FALLBACK_THRESHOLD=0.40.

Focus areas:
1. Kalman filter correctness (predict/update equations, clamping to G_MIN/G_MAX)
2. SQL query safety and correctness (median computation, session grouping, task_type exclusion)
3. Control law math (threshold = FALLBACK + K*(G_BASELINE - g), clamped to [64000/ctx_len, 0.70])
4. Fallback/error handling completeness (DB missing, config missing, subprocess failure)
5. Test coverage gaps
6. Dynamic context_length reading from config.yaml
7. Hysteresis (0.02 minimum delta before applying config change)

BE TERSE. First line MUST be VERDICT: GO or VERDICT: NO-GO. Then numbered findings tagged [blocking]/[minor]/[nit].

--- DIFF START ---
{diff}
--- DIFF END ---
"""

payload = {
    "model": "kimi-k3:cloud",
    "messages": [
        {"role": "system", "content": "You are a code reviewer. Be terse and technical."},
        {"role": "user", "content": prompt}
    ],
    "max_tokens": 4000,
    "temperature": 0.3,
}

req = urllib.request.Request(
    "http://127.0.0.1:9099/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer dummy",
    },
)

try:
    resp = urllib.request.urlopen(req, timeout=90)
    body = json.loads(resp.read())
    print(f"MODEL: {body.get('model', 'unknown')}")
    print(f"USAGE: {body.get('usage', {})}")
    print("---REVIEW---")
    choice = body["choices"][0]
    content = choice["message"].get("content")
    if not content:
        content = choice["message"].get("reasoning", "(no content - reasoning only)")
    print(content)
    print(f"FINISH: {choice.get('finish_reason', 'unknown')}")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)