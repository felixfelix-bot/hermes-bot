#!/usr/bin/env bash
# pae-savings-check.sh — data collection for pae-savings-watch cron (12h).
# Gathers live facts (board, repo, bench artifacts, processes, proxy DB warm-lane
# usage) and prints them as context for the consultant agent. Read-only.
set -u

echo "=== PAE BOARD (live) ==="
hermes kanban --board pae list 2>/dev/null | head -30 || echo "board query failed"

echo
echo "=== REPO MAIN (pae-caddy) ==="
git -C "$HOME/repos/pae-caddy" log --oneline -5 2>/dev/null || echo "repo missing"
echo "--- remotes ---"
git -C "$HOME/repos/pae-caddy" ls-remote github refs/heads/main 2>/dev/null | awk '{print "github:", $1}'
git -C "$HOME/repos/pae-caddy" ls-remote origin refs/heads/main 2>/dev/null | grep -v ngit | awk '{print "ngit:", $1}'

echo
echo "=== RUNNING PROCESSES (warm lane) ==="
pgrep -af 'warm-gateway|warmgw|llama-server' 2>/dev/null | grep -v grep | head -5 || echo "no warm-lane processes"

echo
echo "=== BENCH / SAVINGS ARTIFACTS (newest first) ==="
find "$HOME/repos/pae-caddy" -iname '*bench*' -newer "$HOME/repos/pae-caddy/docs/adr/0001-adopt-pae-local-inference-lane.md" 2>/dev/null | head -10
ls -la "$HOME/repos/pae-caddy/examples/benchattest/bundle/artifacts/" 2>/dev/null | tail -5
find "$HOME/repos/pae-caddy" -name '*prefill*' -o -name '*savings*' 2>/dev/null | head -10
echo "--- PAE-7 result capture ---"
sqlite3 "$HOME/.hermes/kanban/boards/pae/kanban.db" "SELECT substr(coalesce(result,''),1,600) FROM tasks WHERE id='t_597c63a2'" 2>/dev/null || echo "no result"

echo
echo "=== PROXY DB: warm/pae lane usage (24h) ==="
sqlite3 -readonly "$HOME/.hermes/bot/zai_usage.db" "
SELECT key_name, COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0)
FROM api_calls
WHERE ts > strftime('%s','now')-86400
  AND (key_name LIKE '%pae%' OR key_name LIKE '%warm%' OR key_name LIKE '%llama%')
GROUP BY key_name LIMIT 10;" 2>/dev/null || echo "no warm-lane rows (expected until PAE-6 pilot routes traffic)"

echo
echo "=== PAE-4..7 LAST COMMENTS (evidence trail) ==="
for t in t_d7653106 t_8de55b3b t_6ceef595 t_597c63a2; do
  echo "--- $t ---"
  sqlite3 "$HOME/.hermes/kanban/boards/pae/kanban.db" \
    "SELECT datetime(created_at,'unixepoch','localtime') || ' | ' || substr(body,1,250)
     FROM task_comments WHERE task_id='$t' ORDER BY created_at DESC LIMIT 1" 2>/dev/null || echo "none"
done
echo "=== END DATA ==="
