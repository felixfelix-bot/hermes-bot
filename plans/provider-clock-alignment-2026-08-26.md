# Plan: Provider Clock Alignment for Quota Windows

**Date:** 2026-08-26
**Status:** DONE
**Scope:** Align weekly/monthly quota windows to each provider's actual reset clock. Session windows stay rolling (per user decision). opencode_go measured empirically.

## Checklist

### Phase A — Discovery (read-only)

- [x] A1: Resolve api_calls.ts epoch basis (+11h offset BLOCKER)
  - **RESOLVED: no bug.** Writer uses `time.time()` (UTC epoch); probe confirms delta <0.2s.
  - The "+11h offset" was a conversation-drift artifact (system clock advanced past midnight UTC between queries while I cached earlier `date -u` output).

- [x] A2: Ollama weekly anchor — capture from dashboard "resets in N days" text
  - **RESOLVED: rolling 7d IS their definition.** Anchored window (Mon-Mon) gave 19.98% vs dashboard's 38.8% — anchored made things WORSE. Rolling 7d gives 40.75% (matches within 2%). No anchor needed.

- [x] A3: z.ai anchors (API-exposed, already verified live)
  - **RESOLVED:** ours weekly nextResetTime = 2026-08-27 13:47:58 UTC (fetched live from API). Stored in registry.
  - Friend key deactivated (ZAI_API_KEY absent from all .env files; key_health shows dispatch_fail 5).
  - 5h session: NO nextResetTime published by z.ai → stays rolling (per user decision).
  - Monthly TIME_LIMIT entry didn't appear in current API response (only weekly + monthly windows; monthly may not apply to our tier).

- [x] A4: opencode_go — measure empirically via allowance logger
  - Logger implemented (B3). Will gather (ts, remaining_usd) data per response; anchor inferred when allowance jumps back up (needs days/weeks of data).

### Phase B — Implementation

- [x] B1: Created `~/.hermes/bot/src/quota_clock.py`
  - Per-provider anchor registry: `~/.hermes/bot/quota_clock_state.json`
  - `window_start(provider, kind, now) -> epoch`
  - `next_reset(provider, kind) -> ts`
  - `register_anchor(provider, kind, ts)`
  - `fetch_zai_anchors(api_key) -> {kind: ts}` (parses nextResetTime from API response)
  - `refresh_zai_anchors(api_key, provider_name) -> int refreshed`
  - Precedence: API-fetched > rolling fallback (no learned anchor needed — z.ai publishes exact nextResetTime)
  - Kill-switch: `QUOTA_CLOCK_ALIGN_ENABLED=false`

- [x] B2a: zai_proxy._refresh_loop now refreshes z.ai anchors every 5min cycle
  - At line 4113+: after fetching quota windows, calls `quota_clock.refresh_zai_anchors(key, name)` for each key in KEYS. Idempotent + best-effort.

- [x] B2b: price_viz.render_ascii() shows "Resets" column
  - `ours: 1.0d` (matches z.ai weekly anchor Aug 27 13:47 UTC, ~1 day from now)
  - `load_current_quota_state()` uses `window_start()` for weekly windows (anchored when known, rolling fallback)

- [x] B3: opencode_go allowance logger in zai_proxy
  - At line 4815+: appends `{"ts": ..., "remaining_usd": ...}` to `~/.hermes/bot/opencode_allowance_log.jsonl`
  - Bounded to ~30KB (rotates to last 500 lines)
  - Anchor-fitting deferred until ≥1 jump observed

- [x] B4: 48h shadow validation logger
  - price_viz.py main render path logs rolling vs anchored weekly % to `~/.hermes/viz/shadow-comparison.jsonl`
  - First entry: `ours` anchored 100% vs rolling 100% (they agree because ours is exhausted on both definitions)
  - Runs hourly via existing price_viz cron → 48h of data within 2 days

## Rollback
- `QUOTA_CLOCK_ALIGN_ENABLED=false` → all revert to rolling windows
- Registry state file is harmless if unused
- opencode_allowance_log.jsonl is append-only, ignorable
- shadow-comparison.jsonl is append-only, ignorable

## NOT doing (per user decisions)
- No Ollama session clock alignment (rolling stays — matches dashboard within 2%)
- No z.ai 5h session anchoring (no anchor published; per user: weekly anchors only)
- No balance-provider alignment (no windows to align)

## Findings summary
- Ollama weekly IS rolling 7d (dashboard 38.8% vs our rolling 40.75% — within 2%)
- z.ai weekly anchor (nextResetTime) is API-fetched — live, exact
- z.ai session has no published anchor — stays rolling
- opencode_go allowance is response-embedded — fits "balance-style" measurement
- api_calls.ts is correctly UTC — no clock bug
