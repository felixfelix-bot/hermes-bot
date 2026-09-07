# Code-map bake-off: ripwire vs graphify (2026-09-07)

**Goal:** empirically evaluate ripwire (redhat-et/ripwire) and graphify
(Graphify-Labs/graphify) on OUR repos with OUR gold-labelled questions, then
wire the winner fully into hermes (skill + MCP + nightly policy) and leave the
loser installed-but-unwired for a 14-day rollback soak.

**Board task:** router-maintenance t_2c4a0172.
**Executor:** opencode session (me), end-to-end. Manager consultants review
via the board task.

## Why

Current incumbent graphify is limping: nightly `graphify-refresh` cron (02:00,
job id 4f17210fee7c) times out (3600s) every night, only one complete graph
exists (tollgate-rs-spilman, built Jul 16, stale), `graphify-mcp` never wired,
semantic pass degraded (z.ai proxy partial + ollama qwen2.5-coder:3b too slow)
→ AST-only in practice. ripwire claims 58.3% vs graphify 31.7% strict file@10
on LocBench + 0.31s vs 7.82s index — but those are SELF-published benchmarks
on THEIR corpora. We re-run on ours with gold answers we write ourselves
BEFORE running any tool.

## Decision criteria (pre-registered — do not move the goalposts)

Primary: **strict file@10** on our gold set (all gold files for a question
present in top-10 ranked output).
Tiebreak 1: **bytes-to-answer** (tokens ≈ bytes/4) vs the grep-and-read
baseline arm (what agents do today).
Tiebreak 2: **ops** (index/cold/warm latency, refresh cost after a commit,
reliability, nightly maintenance burden).

**Decision rule:**
- ≥10pp lead in strict file@10 → outright win.
- Within 10pp → lower median bytes-to-answer wins.
- Still tied → ops decides (note: a ripwire win means NO nightly job at all).
- Both <50% → neither ships; keep grep baseline; investigate.

## Repo set (user-approved)

1. `~/repos/tollgate-rs-spilman` — Rust, 49MB, clean repo, has stale graph.
2. `~/.hermes/bot` — Python, our daily maintenance reality.
3. Messy third: balloon-fresh (1.9GB, mixed Python/firmware/assets, crawl
   guards required) — verify plebeian-market existence first; balloon-fresh is
   the default.

## Fairness rules

- Code-only mode for BOTH arms (ripwire has no LLM pass by nature; graphify
  `--code-only`) — no proxy-dependent semantic pass in scoring runs.
- Each question TYPE maps to each tool's best-suited command (documented in
  the harness); the mapping is fixed before results are seen.
- Both tools at latest stable versions at eval time; versions recorded in
  results.
- Optional appendix (not scored): one capped graphify run WITH semantic pass on
  one repo, to note what it adds vs costs.

---

## Phase 0 — fair setup (isolation, versions) — ~30 min

- [x] Upgrade `graphifyy` 0.9.17 → latest via uv; record version
- [x] Clone ripwire to `~/repos/ripwire` (NOT in bot repos.txt — no nightly
      side effect); read `install.sh` + CMake options BEFORE running anything
- [x] Build ripwire from release tag manually (gcc 15.2 OK for C++23);
      audit any auto-skill behavior (installer writes skills for every agent
      found — check flags, avoid or prune)
- [x] Verify third repo choice (plebeian-market exists? language?) else
      balloon-fresh with crawl guards (`.graphifyignore` for assets)
- [x] Record: tool versions, build paths, `which ripwire graphify`
- [x] ISOLATION: nothing touches the live nightly cron or hermes config until
      Phase 5

## Phase 1 — gold query set (~1.5 h) — the core

- [x] ~36-40 questions total, 12-14 per repo, typed:
      orient (2), where-is-X (4), who-calls-Y (3), blast-radius (2),
      what-tests-cover-Z (2), recent-change-impact (1-2) per repo
- [x] Sources for gold: recent board-task fixes + git log (files actual fixes
      touched) + grep verification. Gold answers written BEFORE any tool run.
- [x] Each gold entry cites file paths (+ symbols); sanity pass: every gold
      path exists at HEAD at eval time
- [x] Stored as `tools/bakeoff/gold_questions.jsonl`
- [x] Question phrasing deliberately agent-realistic (not adapted to either
      tool's flag syntax)

## Phase 2 — harness + runs (~1.5 h)

- [x] `tools/bakeoff/runner.py`: 3 arms per question —
      ripwire (qtype → `--for` / `--callers` / `--impact` / `--test-gate`),
      graphify (qtype → `query` / `explain` / `path`),
      grep-and-read baseline (`rg` keyword + read top-hit files whole)
- [x] Index step per repo per tool (`--code-only` for graphify; ripwire cold
      parse) — record cold + warm timings
- [x] Capture per question: wall time (median of 3), stdout bytes, ranked
      file list vs gold, exit codes, empties/errors
- [x] Emblematic outputs saved under `tools/bakeoff/results/raw/`
- [x] Results table written: per-repo + aggregate; versions recorded; the
      decision doc
- [x] NOTE: ripwire outputs in a scratch location where possible; audit what
      files each tool leaves in the repo (flag in results)

## Phase 3 — freshness + robustness (~30 min)

- [x] Scratch clone of tollgate (not the real repo): commit touching one
      function + add a new function; run each tool's refresh; does it
      surface? at what time cost?
- [x] Hostile cases (scratch dir): shallow clone/no git history, file with
      syntax error, empty dir, language-skipped file — do tools disclose or
      die quietly?

## Phase 4 — end-to-end hermes agent test (~30 min)

- [x] 10 gold questions (fresh phrasing) via hermes one-shot with the tool's
      invocation exposed in the prompt (skill-style instruction)
- [x] Count: correct answers, tool invocations, tokens used
- [x] This is the "wired in and working" dress rehearsal BEFORE wiring

## Phase 5 — wire in the winner (~1 h)

- [x] Skill: curated skill in manager profile (+ shared skills dir if
      applicable); graphify also has native `--platform hermes` support;
      ripwire skills audited + curated
- [x] MCP: winner's server added to hermes `mcp_servers` (config.yaml ~1043)
      as stdio spawn (pattern: ngit-tool, command + args, per-session spawn)
      — graphify: `python -m graphify.serve <graph.json>` (multi-graph via
      contexts, GRAPHIFY_MAX_CONTEXTS default 8); ripwire: verify MCP
      invocation from its repo (.mcp.json)
- [x] Nightly policy: ripwire wins → DELETE graphify-refresh nightly (no
      persistent index to refresh, ~0.3s parses); graphify wins → rewrite
      nightly to `--code-only` always, drop the proxy semantic pass (timeout
      suspect), per-repo time budget + overrun alert
- [x] Loser: stays installed-but-unwired (14-day rollback window, then
      uninstall)
- [x] Update board t_2c4a0172 with results table + decision + wiring notes

## Phase 6 — soak + persist (passive + commit)

- [x] Commit all bake-off artifacts (plan, gold set, runner, results) via
      branch dance to dr main
- [ ] Soak checklist (7 nights if a nightly exists): completes under budget
      7/7, agent adoption spot-checks, revert path intact until day 14
- [ ] Day-14 follow-up: uninstall loser (board task reminder)

---

## Notes / risks

- Benchmarks on both sides are self-published → our own gold set is the point.
- graphify semantic pass excluded from scoring: it's the nightly timeout
  suspect + costs tokens; our fleet uses AST-only in practice anyway.
- Driver-skill mismatch risk: mapping per qtype documented in harness, fixed
  before results.
- ripwire installer auto-skill behavior: audit before running; prune writes we
  did not want; note in results.
- Do not commit `graphify-out/` or tool caches into the repos.
- The bot repo has a concurrent manager session with in-flight work
  (deepseek cache-hit rate blending) — do not touch its files; commit only
  bake-off artifacts.
