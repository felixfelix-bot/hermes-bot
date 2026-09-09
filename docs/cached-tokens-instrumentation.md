# Cached Tokens Instrumentation (T4)

**Status:** Implemented — `worker-admin/cached-tokens` (awaiting manager merge after T3)
**Date:** 2026-09-09
**Task:** `t_1c18af98` (cost-reduction-sprint)

## Why

Cost audit: provider prompt caches drive a large cost delta. DeepSeek
discounts cached prefixes ~10x, and NW charges real prefill compute for
uncached input. If the agent's volatile state (dates, memory %, quota
numbers) churns the system prompt prefix every turn, provider caches never
hit and the full prefill is paid every call.

The instrumentation gap: `zai_usage.db`'s `api_calls` table logged no
cached-token split — cost analytics could not tell cached from uncached
prompt tokens.

## What changed

### 1. New dedicated column: `api_calls.cached_tokens`

`zai_proxy.py`:

- **Schema**: `cached_tokens INTEGER DEFAULT 0` added to the `api_calls`
  `CREATE TABLE IF NOT EXISTS`.
- **Migration** (`_ensure_api_calls_cached_tokens(conn)`): guarded, idempotent
  `ALTER TABLE api_calls ADD COLUMN cached_tokens INTEGER DEFAULT 0`.
  Duplicate-column errors are swallowed (same pattern as `session_id` /
  `task_type`). **No backfill** — historical rows keep `0` by design because
  the split was never observed before this migration; existing rows are never
  rewritten.
- The migration runs inside `_usage_db()` on first connect, and is exposed as
  a standalone function so it is directly testable against a scratch DB.

### 2. Extraction helper: `_extract_cache_read_tokens(usage)`

Pure function resolving both OpenAI-compatible usage shapes:

- `usage.cache_read_input_tokens` (Anthropic / z.ai style) — wins when both
  are present (source of truth on those responses).
- `usage.prompt_tokens_details.cached_tokens` (OpenAI standard).

Returns `0` when absent. Never raises, never returns `None`.

### 3. Threaded through the response-logging path

`_log_api_call(...)` gained a `cached_tokens=0` parameter, persisted through
the primary INSERT and every guarded fallback (the fallback chain drops the
`cached_tokens`/`task_type` columns progressively for older DBs, so a missing
column never loses the row).

All response-logging call sites now pass `cached_tokens`:

| Site | Source |
|------|--------|
| z.ai primary (`finally` block) | `usage` from `_parse_usage` |
| ollama_cloud fallback | `ollama_usage` |
| opencode_go flat-rate | `og_usage` |
| telnyx | `_telnyx_cached` (already extracted for the pricing ratio) |
| external single / flat-router | `_ext_cached` / `ext_usage` |
| flat-router nested hop | `_usage` |

## Tests

`tests/test_cached_tokens_instrumentation.py` (TDD: RED then GREEN):

- `_extract_cache_read_tokens` resolves both shapes, prefers top-level, `0` on
  absent/None/non-dict/zero.
- Streaming SSE final-chunk usage round-trips through `_parse_usage`.
- Migration adds the column to a legacy DB without touching existing rows.
- Migration is idempotent.
- `_log_api_call(cached_tokens=...)` writes the value (and defaults to `0`).

## Rollback / safety

- The guarded ALTER is non-destructive; dropping the column (`ALTER TABLE
  api_calls DROP COLUMN cached_tokens`) fully reverts the schema.
- The extra `cached_tokens` column is nullable telemetry with a default; its
  absence or failure to log never breaks a request.

## Sequencing

Per the manager's sequencing note (T3 `worker-admin/noop-drop` edits
`zai_proxy.py` concurrently), this change is scoped to the response-logging
section only and lives on the `worker-admin/cached-tokens` branch. Manager
merges T3 first, then this.
