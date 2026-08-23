#!/usr/bin/env bash
# nightly_llms_sweep — generate/update llms.txt for repos flagged by session_registry.
# Called by hermes cron (llms-sweep, 02:00 local). Reads session_registry.json
# to find sessions needing curation. Zero LLM tokens — pure scripting.
set -u
BOT="$HOME/.hermes/bot"
GEN="$BOT/gen_llms_txt.py"
REG="$BOT/session_registry.py"
PY="$(command -v python3 || echo "$HOME/.hermes/hermes-agent/venv/bin/python")"

echo "=== syncing session registry ==="
"$PY" "$REG" sync 2>&1

echo ""
echo "=== generating curation list ==="
"$PY" - <<'PYLIST' > /tmp/curate-list.tsv
import json
from pathlib import Path
reg = json.loads(Path.home().joinpath(".hermes/bot/session_registry.json").read_text())
for key, wt in reg["sessions"].items():
    if not wt.get("needs_curation"):
        continue
    reasons = wt.get("curation_reasons", [])
    if any("llms_txt" in r for r in reasons):
        print(f"{wt['worktree']}\t{wt['repo']}\t{','.join(reasons)}\t{wt['llms_txt']['path']}")
PYLIST

count=$(wc -l < /tmp/curate-list.tsv 2>/dev/null || echo 0)
echo "  $count sessions need llms.txt curation"

echo ""
echo "=== processing ==="
generated=0
updated=0

while IFS=$'\t' read -r worktree repo reason llms_path; do
  [ -z "$worktree" ] && continue
  if [ ! -f "$llms_path" ]; then
    echo "  [$repo] generating new llms.txt ($reason)"
    "$PY" "$GEN" "$worktree" > "$llms_path" 2>/dev/null
    if [ -s "$llms_path" ]; then
      generated=$((generated + 1))
    else
      rm -f "$llms_path"
      echo "    → FAILED (empty output)"
    fi
  else
    echo "  [$repo] updating stale llms.txt ($reason)"
    "$PY" "$GEN" "$worktree" > "$llms_path" 2>/dev/null
    if [ -s "$llms_path" ]; then
      updated=$((updated + 1))
    fi
  fi
done < /tmp/curate-list.tsv

rm -f /tmp/curate-list.tsv
echo ""
echo "llms-sweep done: $generated new, $updated updated"
