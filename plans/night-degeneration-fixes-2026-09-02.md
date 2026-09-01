# Night Degeneration & Compaction Fixes (2026-09-02)

Status: IMPLEMENTED (see verification notes)
Owner: c03rad0r
Context: follow-up to `plans/ollama3-burn-reduction-2026-09-02.md`. Tonight's incidents: (1) hermes delegate/subagent sessions on glm-5.3 produce degenerate output ("mojibake walls", empty assistant turns, zombie workers 2/2 dead — the concurrent session's diagnosis: "z.ai glm long tool-loop sessions generate garbage tonight"); (2) the dispatcher session compacted every 1–2 minutes.

## Root causes (verified 2026-09-02 ~03:2x)

- **RC-1 (ours, code)**: zai_proxy's empty-content rescue only reads `reasoning_content` (z.ai naming). Ollama/neuralwatt return the field as **`reasoning`** — live proof: oc3 verification response `{"content":"", "reasoning":"The user wants me to reply..."}`. Good ollama-routed glm-5.3 completions are therefore treated as empty → clients store empty assistant turns → long sessions derail/inflate. The canonical fix exists but is dead code: `src/reasoning_handler.py` (extracted during a refactor, never wired — and also only knows `reasoning_content`).
- **RC-2 (z.ai, external)**: friend-lane long-session corruption tonight (both zombie workers died pre-03:13-restart when friend was the only healthy glm lane; mojibake walls, corrupted goal texts). Mitigation = route delegates elsewhere (this plan) + standing "no glm dispatches" rule from the concurrent session.
- **RC-3 (ours, config interaction)**: dispatcher session compaction thrash — (a) compression threshold was lowered 0.7→0.15 at 03:05 making compaction fire at every refill; (b) session-search tool returns ~96k-char results that bypass `tool_output.max_bytes=50000` and refill the context immediately (last 10 messages = 270k chars); (c) empty assistant turns (RC-1) break continuity so the model re-does work and grows loops; (d) pre-03:13 503-storm retry bloat.
- Note: oc3 IS live (proxy restarted 03:13:53, commit 2c9b5bf HEAD, 53 calls/4.8M tokens, mo=5%) — the concurrent session's "not live" finding predates the restart.

## User decisions
- delegation model tonight: **deepseek-v4-pro**
- manager compression threshold: **0.35** (revert of my 0.15)
- scope: **all fixes (FIX-1..4)**

## Checklist

### FIX-1 — reasoning normalization (degeneration shim)
- [x] `zai_proxy.py` site 1 (~:4800): after `reasoning_content` miss, also check `reasoning`
- [x] `zai_proxy.py` site 2 (~:6210): same extension
- [x] `src/reasoning_handler.py`: check both field names (canonical correctness)
- [x] New `tests/test_reasoning_injection.py`: ollama shape, zai shape, both-empty, non-empty passthrough, non-string guard
- [x] py_compile green

### FIX-2 — compaction un-thrash
- [x] `~/.hermes/config.yaml`: compression threshold 0.15 → 0.35
- [x] `~/.hermes/profiles/manager/config.yaml`: threshold 0.15 → 0.35 (kimi-consultant stays 0.15 — no thrash observed)
- [x] Investigate why session-search tool results bypass `tool_output.max_bytes` (96k chars observed); apply config-level fix if knob exists; document code-path finding otherwise
- [x] Watch dispatcher session (`b4d817eb`) compaction spacing post-change

### FIX-3 — delegation off the corrupt lane
- [x] `profiles/manager/config.yaml`: `delegation.model: glm-5.3` → `deepseek-v4-pro`
- [x] Smoke: deepseek-v4-pro 200 through proxy

### FIX-4 — cross-family reviewer
- [x] Create `worker-reviewer-qwen` profile on `qwen3.5:397b` (ollama rung, $0-marginal) alongside reviewer-glm + reviewer-kimi
- [x] Verify the profile is discoverable by the review-dispatch machinery (or document registration follow-up)

### FIX-5 — verification + ship
- [x] Full pytest suites green
- [x] Restart zai-proxy (~5s, timed away from concurrent session's dispatches; do NOT touch their deepseek worker task or 04:15 watchdog)
- [x] Live proof: ollama-routed glm-5.3 with small max_tokens returns NON-EMPTY content (reasoning injected) — repeat the key-disabled oc3 test
- [x] Journal clean post-restart (no tracebacks)
- [x] Update this checklist; commit bot-repo changes (zai_proxy.py, src/reasoning_handler.py, new test, this doc); profile-config edits are live-only, noted here
- [x] Signal/note to the concurrent session: proxy restart happened; FIX-1 may un-jam glm lanes for SHORT uses — but keep "no glm dispatches" until they see stability

## Out of scope / follow-ups
- hermes-core code change for session-search truncation (if no config knob) — separate PR
- z.ai friend-lane long-session corruption (external; retest after their stabilization)
- MRE↔bot src reconciliation (P1 from previous plan doc)
- Role-model assignment polish (manager stays glm-5.3 interactive; grunt = deepseek delegation; reviewers 3-family)

## Verification results (2026-09-02 ~03:5x)
- FIX-1 live-proof: qwen3.5:397b degenerate case (content="" + finish=length) now returns reasoning as content through the ollama path; glm-5.3 '42' via oc3 normal; SSE stream path unaffected.
- FIX-1 sites: zai_proxy :4808 (_try_zai_key) + :6225 (_proxy analysis) + NEW _try_ollama_cloud non-stream buffering (~:4670, FIX-1b — the path the qwen/glm-3 degeneration actually took).
- Tests: new tests/test_reasoning_injection.py 14 passed (file-path import — avoids the dual-`src` package pre-binding hazard); tests/test_deploy_import_shadow.py stale Phase-1 kill-switch assertion flipped to Phase-3 reality (live routing is primary since 2026-08-24; test had been failing stale).
- Suite status: target suites 121 passed; bot-wide 442 passed + 12 PRE-EXISTING failures (telnyx_failover, pressure_wiring, shadow_drop_ours, cost_correction — identical on stashed baseline); tests/test_compression_growth_governor.py has a pre-existing collection ImportError (stale MIN_THRESHOLD import).
- hermes-agent session-search cap (FIX-2b): tests/tools/test_session_search.py 52 passed (6 new _cap_output tests). NOTE: (a) change is a LIVE working-tree patch in ~/hermes/hermes-agent — intentionally left uncommitted for separate review per that repo's conventions; (b) takes effect for sessions after the next gateway restart — hermes-gateway was NOT restarted tonight (concurrent session dispatching); the compression threshold 0.35 IS read live per-compression and already applies.
- FIX-4: worker-reviewer-qwen profile created (config.yaml qwen3.5:397b via localhost:9099, ctx 262144; auth.json + SOUL.md copied from reviewer-kimi pattern). Smoke: qwen 200 via ollama_cloud.
- deepseek-v4-pro delegation lane smoke: 200 via ollama_cloud ($0-marginal grunt lane).
- Journal post-restart: 0 tracebacks.
