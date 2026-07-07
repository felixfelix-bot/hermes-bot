#!/usr/bin/env bash
# llms_txt_nightly — fill llms.txt gaps across all tracked repos (D-040).
#
# For every repo in repos.txt that lacks a TRACKED llms.txt (and doesn't
# gitignore it), generate one via gen_llms_txt.py and commit it on the current
# branch. NEVER pushes — push is human-gated per TODO L95 (upstream-collaborator
# approval is per-repo).
#
# Watchdog pattern: SILENT when there is nothing to do (every repo already has a
# tracked llms.txt). Prints a one-line summary ONLY when it created >=1 file, so
# a quiet cron run means "all repos already oriented". Intended to run via a
# Hermes no-agent cronjob (stdout is delivered verbatim; empty = silent).
set -u

HOME_DIR="${HOME:-/home/c03rad0r}"
BOT="$HOME_DIR/.hermes/bot"
REPOS="$BOT/repos.txt"
GEN="$BOT/gen_llms_txt.py"
# fall back to the versioned repo copy if the deployed canonical copy is absent
[ -f "$GEN" ] || GEN="$HOME_DIR/hermes-orchestration/scripts/engine/gen_llms_txt.py"

[ -f "$REPOS" ] || { echo "llms-nightly: $REPOS missing — run ansible bootstrap (make ansible)"; exit 0; }
[ -f "$GEN" ]   || { echo "llms-nightly: gen_llms_txt.py not found"; exit 0; }

created=0; created_list=""; skipped_ignored=0; failed=""
while IFS= read -r p; do
  p="${p%%#*}"                                  # strip inline comments
  p="${p#"${p%%[![:space:]]*}"}"; p="${p%"${p##*[![:space:]]}"}"  # trim whitespace
  [ -z "$p" ] && continue
  [ -d "$p" ] || continue
  # already committed in HEAD (curated or previously generated)? done — preserves
  # human edits. NOTE: we check HEAD (not the index) so a staged-but-uncommitted
  # file from a prior failed commit is retried, not silently skipped forever.
  if git -C "$p" ls-tree -r HEAD -- llms.txt 2>/dev/null | grep -q .; then continue; fi
  # present on disk but gitignored? respect the owner's per-repo choice
  if [ -f "$p/llms.txt" ] && git -C "$p" check-ignore -q llms.txt 2>/dev/null; then
    skipped_ignored=$((skipped_ignored + 1)); continue
  fi
  name="$(basename "$p")"
  # generate ONLY if absent; if a file exists but is uncommitted, commit it as-is
  # (never clobber a human's staged edits).
  if [ ! -f "$p/llms.txt" ]; then
    python3 "$GEN" "$p" > "$p/llms.txt" 2>/dev/null || { failed="${failed:+$failed }$name"; continue; }
  fi
  if git -C "$p" add llms.txt 2>/dev/null \
     && git -C "$p" commit -m "Add llms.txt (LLM orientation map, llmstxt.org; D-040) [nightly]" -- llms.txt >/dev/null 2>&1; then
    created=$((created + 1)); created_list="${created_list:+$created_list }$name"
  else
    # unstage on failure so the next run retries cleanly instead of seeing a
    # staged-but-uncommitted file as "done"
    git -C "$p" reset -q HEAD -- llms.txt 2>/dev/null
    failed="${failed:+$failed }$name"
  fi
done < "$REPOS"

# Report only when there is something notable; stay silent when every repo is
# already oriented (clean cron = nothing to do).
if [ "$created" -gt 0 ] || [ -n "$failed" ]; then
  msg="llms.txt nightly:"
  [ "$created" -gt 0 ] && msg="$msg generated+committed $created repo(s): ${created_list}."
  [ -n "$failed" ]      && msg="$msg FAILED to commit: ${failed}."
  [ "$skipped_ignored" -gt 0 ] && msg="$msg (${skipped_ignored} gitignored, respected.)"
  msg="$msg Not pushed — push is human-gated (TODO L95)."
  echo "$msg"
fi
exit 0
