#!/usr/bin/env bash
# nightly_sweep.sh — off-peak (02:00 local) upkeep sweep.
#
# Regenerates the three manager-facing indexes the bot reads at dispatch time:
#   1. llms.txt gap-fill  — delegated to llms_txt_nightly.sh (git-aware: respects
#                           tracked/gitignored llms.txt, commits new ones, never
#                           pushes — push is human-gated per D-040/TODO L95).
#                           Runs gen_llms_txt.py across all repos in repos.txt.
#                           Falls back to an inline no-commit gap-fill loop if
#                           llms_txt_nightly.sh is absent.
#   2. handovers/INDEX.md — worktree → handover map (gen_handovers.py).
#   3. synergy_map.json   — repo themes + overlap candidates (synergy_map.py).
#
# Idempotent and step-independent: one step failing does NOT abort the others;
# the wrapper exits non-zero only if a step errored, so systemd surfaces it.
set -u

HOME_DIR="${HOME:-/home/c03rad0r}"
BOT="$HOME_DIR/.hermes/bot"
REPOS="$BOT/repos.txt"
PY="$(command -v python3 || echo python3)"
rc=0
ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "[$(ts)] nightly sweep start (bot=$BOT)"

# --- 1. llms.txt gap-fill -------------------------------------------------
if [ -x "$BOT/llms_txt_nightly.sh" ]; then
  # Canonical git-aware filler (commits new llms.txt, silent on no-op).
  out="$("$BOT/llms_txt_nightly.sh" 2>&1)" || rc=1
  if [ -n "$out" ]; then echo "[$(ts)] llms.txt: $out"; else echo "[$(ts)] llms.txt: all repos already oriented (no-op)"; fi
elif [ -f "$BOT/gen_llms_txt.py" ] && [ -f "$REPOS" ]; then
  # Fallback: inline gap-fill, write-only (never commits), curated preserved.
  gen=0; skip=0
  while IFS= read -r r; do
    [ -z "$r" ] && continue
    case "$r" in '#'*) continue;; esac
    [ -d "$r" ] || continue
    if [ -f "$r/llms.txt" ]; then
      skip=$((skip + 1))
    else
      if "$PY" "$BOT/gen_llms_txt.py" "$r" > "$r/llms.txt" 2>/dev/null; then
        gen=$((gen + 1))
      else
        echo "[$(ts)]   WARN: gen_llms_txt failed for $r"
        rm -f "$r/llms.txt"
        rc=1
      fi
    fi
  done < "$REPOS"
  echo "[$(ts)] llms.txt: $gen generated, $skip already present (inline fallback; curated preserved)"
else
  echo "[$(ts)] SKIP llms.txt step (neither llms_txt_nightly.sh nor gen_llms_txt.py + repos.txt present)"
fi

# --- 2. handovers index ---------------------------------------------------
if [ -f "$BOT/gen_handovers.py" ]; then
  if "$PY" "$BOT/gen_handovers.py"; then
    echo "[$(ts)] handovers OK"
  else
    echo "[$(ts)] WARN: gen_handovers failed"
    rc=1
  fi
else
  echo "[$(ts)] SKIP handovers step (gen_handovers.py absent)"
fi

# --- 3. synergy map -------------------------------------------------------
if [ -f "$BOT/synergy_map.py" ]; then
  if "$PY" "$BOT/synergy_map.py"; then
    echo "[$(ts)] synergy_map OK"
  else
    echo "[$(ts)] WARN: synergy_map failed"
    rc=1
  fi
else
  echo "[$(ts)] SKIP synergy step (synergy_map.py absent)"
fi

echo "[$(ts)] nightly sweep done (rc=$rc)"

# --- 4. git commit state changes (version tracking) ---------------------
STATE_REPO="$HOME_DIR/hermes-orchestration"
if [ -d "$STATE_REPO/.git" ]; then
  cd "$STATE_REPO" || true
  git add state/ 2>/dev/null
  if ! git diff --cached --quiet 2>/dev/null; then
    git commit -m "state-sync: $(date -u +%FT%T)Z — nightly sweep" 2>/dev/null
    git push 2>/dev/null && echo "[$(ts)] state pushed to ngit" || echo "[$(ts)] state committed (push skipped)"
  fi
fi

exit "$rc"
